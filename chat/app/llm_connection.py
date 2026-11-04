"""Process-local Chat routing. No settings file, secret persistence or model probes."""
import asyncio, copy, ipaddress, os, re, secrets, socket
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from typing import Any
import httpx
from app.errors import APIError
from app.model_config import ModelConfig, normalize_model

ACTIVE_CONNECTION = ContextVar('chat_connection', default=None)
DEFAULT_URL = 'https://openrouter.ai/api/v1'
FIELDS = {'kind','baseUrl','model','auth','secretRef','connectTimeout','readTimeout'}

def canonical_url(value):
    try:
        if not isinstance(value,str):raise ValueError()
        p=urlsplit(value)
        if p.scheme not in {'https','http'} or not p.hostname or p.username is not None or p.password is not None or p.query or p.fragment or p.port==0 or '%' in p.netloc or '\\' in value or any(c.isspace() for c in value):raise ValueError()
        url=str(httpx.URL(value)).rstrip('/')
        return url+'/v1' if not p.path or p.path=='/' else url
    except (ValueError,TypeError,httpx.InvalidURL):raise APIError(400,'인증정보나 query 없는 추론 API 주소를 입력하세요.') from None

def destination(url):
    p=urlsplit(url)
    if p.username is not None or p.password is not None:raise APIError(400,'URL에 인증정보를 넣지 마세요.')
    local=p.hostname in {'127.0.0.1','::1','localhost'}
    if local and os.getenv('AI_ENVIRONMENT') in {'local','develop'}:return
    if p.scheme!='https':raise APIError(400,'원격 추론 API는 HTTPS 주소를 사용하세요.')
    try:
        address=ipaddress.ip_address(p.hostname)
    except ValueError:return
    if not address.is_global:raise APIError(400,'메타데이터 및 사설 네트워크 주소는 사용할 수 없습니다.')

def validate(value) -> dict[str, Any]:
    if not isinstance(value,dict) or set(value)-FIELDS:raise APIError(400,'연결 설정 형식을 확인하세요.')
    c=copy.deepcopy(value)
    if c.get('kind') not in ('openrouter','openai-compatible'):raise APIError(400,'연결 방식을 확인하세요.')
    c['baseUrl']=canonical_url(c.get('baseUrl',''));destination(c['baseUrl'])
    if not isinstance(c.get('model'),str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:-]{0,159}',c['model']):raise APIError(400,'모델 ID를 확인하세요.')
    if c['kind']=='openrouter':
        if c['baseUrl']!=DEFAULT_URL:raise APIError(400,'OpenRouter는 기존 API 주소를 사용합니다.')
        try:c['model']=normalize_model(c['model'])
        except ValueError:raise APIError(400,'OpenRouter 모델 ID를 확인하세요.') from None
        c['auth']='existing-openrouter'
    else:
        if c['baseUrl']==DEFAULT_URL:raise APIError(400,'OpenRouter 연결을 선택하세요.')
        c['auth']='session-key'
    c['secretRef']=None
    for key,default,maximum in [('connectTimeout',10,30),('readTimeout',75,120)]:
        n=c.get(key,default)
        if type(n) is not int or not 1<=n<=maximum:raise APIError(400,'시간 제한 범위를 확인하세요.')
        c[key]=n
    return c

@dataclass(frozen=True)
class AdmittedConnection:
    kind:str
    base_url:str
    model:str
    revision:int
    client:httpx.AsyncClient | None=field(default=None,repr=False,compare=False)
    connect_timeout:int=10
    read_timeout:int=75
    async def close(self):
        if self.client is not None:
            try:await self.client.aclose()
            finally:self.client.headers.pop('Authorization',None)
    def public(self):return {'kind':self.kind,'baseUrl':self.base_url,'model':self.model,'revision':self.revision}
    def model_config(self):return ModelConfig(self.model,frozenset({'tools','tool_choice','temperature','max_tokens'}),{},False,1200)

class PinnedTransport(httpx.AsyncBaseTransport):
    """Resolve each destination, then connect to that public IP with original TLS/Host.

    httpx handles redirects, including Modal's 303 result GET, without replaying
    the original POST. The client removes credentials on cross-origin redirects.
    """
    def __init__(self):self.inner=httpx.AsyncHTTPTransport(retries=0)
    async def handle_async_request(self,request):
        destination(str(request.url))
        try:
            addresses=await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo,request.url.host,request.url.port or 443,type=socket.SOCK_STREAM),request.extensions.get('timeout',{}).get('connect',10))
        except TimeoutError:raise httpx.ConnectTimeout('추론 API DNS 연결 시간이 초과됐습니다.',request=request) from None
        except OSError:raise APIError(503,'추론 API 주소의 DNS를 확인하지 못했습니다.') from None
        ips=sorted({str(x[4][0]) for x in addresses})
        local=request.url.host in {'127.0.0.1','::1','localhost'} and os.getenv('AI_ENVIRONMENT') in {'local','develop'}
        if not ips or not local and any(not ipaddress.ip_address(x).is_global for x in ips):raise APIError(400,'메타데이터 및 사설 네트워크 주소는 사용할 수 없습니다.')
        pinned=httpx.Request(request.method,request.url.copy_with(host=ips[0]),headers=request.headers,stream=request.stream,extensions={**request.extensions,'sni_hostname':request.url.host})
        return await self.inner.handle_async_request(pinned)
    async def aclose(self):await self.inner.aclose()

class Connections:
    def __init__(self):
        self.lock=asyncio.Lock()
        self.revision=secrets.randbits(48)+1
        self.default=validate({'kind':'openrouter','baseUrl':DEFAULT_URL,'model':os.getenv('CHAT_MODEL','openai/gpt-6-luna')})
        self._active=None;self._key=None
    def read(self):return {'revision':self.revision,'config':copy.deepcopy(self._active or self.default)}
    def openrouter_ready(self):
        try:
            from integration.provider_key import ProviderKey
            ProviderKey.from_environment('chat').snapshot()
            return True
        except Exception:return False
    def require_openrouter(self):
        if not self.openrouter_ready():raise APIError(503,'기본 OpenRouter 역할별 키를 준비하세요.')
    def public(self):
        return {**self.read(),'defaultConfig':copy.deepcopy(self.default),'memoryOnly':True,'endpointActive':self._active is not None,'openrouterReady':self.openrouter_ready()}
    def same_revision(self,revision):
        if type(revision) is not int or revision!=self.revision:raise APIError(409,'서버가 재시작됐거나 설정이 바뀌었습니다. 현재 연결을 다시 조회하세요.')
    async def activate(self,c,key,revision):
        c=validate(c)
        if c['kind']=='openrouter':return await self.disable(revision)
        # Header shape only, not a vendor-specific token format. Never echo input.
        if not isinstance(key,str) or len(key)>4096 or any(ord(x)<33 or ord(x)>126 for x in key):raise APIError(400,'추론 API 키 형식을 확인하세요.')
        async with self.lock:
            self.same_revision(revision);self.require_openrouter()
            if self._active==c and self._key==key:return self.public()
            self._active=c;self._key=key;self.revision+=1
            return self.public()
    async def disable(self,revision):
        async with self.lock:
            self.same_revision(revision)
            self._active=None;self._key=None;self.revision+=1
            return self.public()
    async def client(self,c,transport=None,*,token=None):
        origin=httpx.URL(c['baseUrl'])
        async def guard(request):
            # Permit ordinary redirects, but never forward a key to another host
            # or port and never downgrade a remote connection to cleartext.
            destination(str(request.url))
            if (request.url.scheme,request.url.host,request.url.port)!=(origin.scheme,origin.host,origin.port):request.headers.pop('Authorization',None)
        headers={'Authorization':'Bearer '+token} if token else {}
        return httpx.AsyncClient(base_url=c['baseUrl'].rstrip('/')+'/',headers=headers,timeout=httpx.Timeout(c['readTimeout'],connect=c['connectTimeout']),trust_env=False,follow_redirects=True,max_redirects=5,event_hooks={'request':[guard]},transport=transport or PinnedTransport())
    async def admit(self):
        # Capture before the first await. Off/switch doesn't change admitted calls.
        c=copy.deepcopy(self._active or self.default);key=self._key;revision=self.revision
        if c['kind']=='openrouter':
            self.require_openrouter()
            return AdmittedConnection('openrouter',c['baseUrl'],c['model'],revision,connect_timeout=c['connectTimeout'],read_timeout=c['readTimeout'])
        client=await self.client(c,token=key)
        return AdmittedConnection(c['kind'],c['baseUrl'],c['model'],revision,client,c['connectTimeout'],c['readTimeout'])
