"""Revisioned Chat-only provider settings. Credentials remain process-local references."""
import asyncio, copy, hashlib, ipaddress, json, os, re, socket, tempfile, time, fcntl
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from app.errors import APIError
from app.model_config import ModelConfig, normalize_model

ACTIVE_CONNECTION = ContextVar('chat_connection', default=None)
FIELDS = {'kind','baseUrl','model','auth','secretRef','connectTimeout','readTimeout'}
DEFAULT_URL = 'https://openrouter.ai/api/v1'

def canonical_url(value):
    try:
        p=urlsplit(value)
        if p.scheme not in {'https','http'} or not p.hostname or p.username is not None or p.password is not None or p.query or p.fragment or p.port==0 or '%' in p.netloc or '\\' in value:raise ValueError()
        if p.path.rstrip('/')!='/v1' and not (p.hostname=='openrouter.ai' and p.path.rstrip('/')=='/api/v1'):raise ValueError()
        if value.rstrip('/')!=f'{p.scheme}://{p.netloc}{p.path.rstrip("/")}':raise ValueError()
        return value.rstrip('/')
    except (ValueError,TypeError):raise APIError(400,'base URL은 인증정보·query가 없는 정확한 /v1 주소여야 합니다.') from None

def destination(url,*,resolve=True) -> tuple[str, ...]:
    """Only deployment-approved destinations; no dashboard-editable network policy."""
    p=urlsplit(canonical_url(url));allowed={x.strip().rstrip('/') for x in os.getenv('CHAT_LLM_ALLOWED_BASE_URLS','').split(',') if x.strip()}
    if url==DEFAULT_URL:return ()
    if url not in allowed:raise APIError(400,'배포 설정의 허용 endpoint 목록에 없는 주소입니다.')
    local=p.hostname in {'127.0.0.1','::1'}
    if local:
        if os.getenv('AI_ENVIRONMENT') not in {'local','develop'}:raise APIError(400,'로컬 시험 주소는 운영에서 사용할 수 없습니다.')
        return (p.hostname,)
    if p.scheme!='https':raise APIError(400,'원격 모델은 HTTPS가 필요합니다.')
    if not resolve:return ()
    try:addresses={str(x[4][0]) for x in socket.getaddrinfo(p.hostname,p.port or 443,type=socket.SOCK_STREAM)}
    except OSError:raise APIError(503,'모델 주소의 DNS를 확인할 수 없습니다.') from None
    if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):raise APIError(400,'사설·메타데이터·로컬 네트워크 모델 주소는 허용되지 않습니다.')
    return tuple(sorted(addresses))

def validate(value):
    if not isinstance(value,dict) or set(value)-FIELDS:raise APIError(400,'알 수 없는 연결 설정입니다.')
    c=copy.deepcopy(value)
    if c.get('kind') not in {'openrouter','openai-compatible'}:raise APIError(400,'지원하지 않는 연결 방식입니다.')
    c['baseUrl']=canonical_url(c.get('baseUrl',''))
    if c['kind']=='openrouter':
        if c['baseUrl']!=DEFAULT_URL or c.get('auth')!='existing-openrouter' or c.get('secretRef'):raise APIError(400,'OpenRouter는 기존 역할별 인증 경로를 사용합니다.')
        try:c['model']=normalize_model(c['model'])
        except (ValueError,KeyError):raise APIError(400,'OpenRouter 모델 ID를 확인하세요.') from None
    else:
        if c['baseUrl']==DEFAULT_URL:raise APIError(400,'공식 OpenRouter 주소는 OpenRouter 연결 방식을 사용하세요.')
        destination(c['baseUrl'],resolve=False)
        if not isinstance(c.get('model'),str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:-]{0,159}',c['model']):raise APIError(400,'모델 ID를 확인하세요.')
        if c.get('auth') not in {'none','secret-ref'}:raise APIError(400,'인증 방식은 none 또는 등록된 비밀 참조입니다.')
        ref=c.get('secretRef') or None
        allowed={x.strip() for x in os.getenv('CHAT_LLM_SECRET_REFS','').split(',') if x.strip()}
        if c['auth']=='secret-ref' and (not ref or ref not in allowed or not os.getenv(ref)):raise APIError(400,'기존 배포 환경의 등록된 비밀 참조가 필요합니다.')
        if c['auth']=='none' and ref:raise APIError(400,'인증 없음에는 비밀 참조를 지정하지 않습니다.')
        c['secretRef']=ref
    for key,default,maximum in [('connectTimeout',10,30),('readTimeout',75,120)]:
        value=c.get(key,default)
        if type(value) is not int or not 1<=value<=maximum:raise APIError(400,'타임아웃 범위를 확인하세요.')
        c[key]=value
    c.setdefault('secretRef',None)
    return c

def fingerprint(c):return hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest()

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
        if self.client is not None:await self.client.aclose()
    def public(self):return {'kind':self.kind,'baseUrl':self.base_url,'model':self.model,'revision':self.revision}
    def model_config(self):return ModelConfig(self.model,frozenset({'tools','tool_choice','temperature','max_tokens'}),{},False,1200)

class PinnedTransport(httpx.AsyncBaseTransport):
    """Connect to a validated IP; preserve original Host and TLS certificate name."""
    def __init__(self,host,address):
        self.host,self.address=host,address
        self.inner=httpx.AsyncHTTPTransport(retries=0)
    async def handle_async_request(self,request):
        pinned=httpx.Request(request.method,request.url.copy_with(host=self.address),headers=request.headers,stream=request.stream,extensions={**request.extensions,'sni_hostname':self.host})
        return await self.inner.handle_async_request(pinned)
    async def aclose(self):await self.inner.aclose()

@contextmanager
def publication_lock(path):
    lock=path.with_name(path.name+'.lock')
    try:
        fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX)
            try:yield
            finally:fcntl.flock(handle,fcntl.LOCK_UN)
    except OSError:raise APIError(503,'설정 쓰기 잠금을 확인할 수 없습니다.') from None

class Connections:
    def __init__(self,path=None):
        self.path=Path(path or os.environ['CHAT_LLM_CONFIG_FILE']) if path or os.getenv('CHAT_LLM_CONFIG_FILE') else None
        self.lock=asyncio.Lock();self.checks={}
        self.default={'kind':'openrouter','baseUrl':DEFAULT_URL,'model':os.getenv('CHAT_MODEL','openai/gpt-6-luna'),'auth':'existing-openrouter','secretRef':None,'connectTimeout':10,'readTimeout':75}
    def read(self):
        if self.path is None or not self.path.exists():return {'revision':0,'config':copy.deepcopy(self.default),'history':[],'verification':{'state':'existing-openrouter-path','connection':None,'tools':None,'sse':None}}
        try:
            if self.path.is_symlink() or self.path.stat().st_size>32000:raise ValueError()
            v=json.loads(self.path.read_text());assert type(v['revision']) is int and v['revision']>0 and isinstance(v['history'],list)
            validate(v['config']);return v
        except (OSError,ValueError,AssertionError,KeyError):raise APIError(503,'모델 연결 설정을 읽을 수 없습니다. 기존 파일을 보존하고 확인하세요.') from None
    def public(self):
        value=self.read();return {**value,'writable':self.path is not None,'allowedBaseUrls':sorted({DEFAULT_URL,*[s.strip() for s in os.getenv('CHAT_LLM_ALLOWED_BASE_URLS','').split(',') if s.strip()]}),'secretReferences':[x.strip() for x in os.getenv('CHAT_LLM_SECRET_REFS','').split(',') if x.strip()],'scope':'chat-only; profiler unchanged','verificationNotice':'도구·SSE 검증과 실제 추천 품질·JSON Schema 지원·배포 성공은 별개입니다.'}
    async def client(self,c,transport=None):
        try:addresses=await asyncio.wait_for(asyncio.to_thread(destination,c['baseUrl']),c['connectTimeout'])
        except TimeoutError:raise APIError(503,'모델 DNS 확인 시간이 초과됐습니다.') from None
        origin=urlsplit(c['baseUrl']);path=origin.path+'/chat/completions'
        async def guard(request):
            if request.method!='POST' or request.url.scheme!=origin.scheme or request.url.host!=origin.hostname or (request.url.port or (443 if request.url.scheme=='https' else 80))!=(origin.port or (443 if origin.scheme=='https' else 80)) or request.url.path!=path or request.url.query or request.url.fragment:raise APIError(503,'허용된 모델 completion 경로만 사용할 수 있습니다.')
            await asyncio.to_thread(destination,c['baseUrl'],resolve=False)
        headers={}
        if c['auth']=='secret-ref':headers['Authorization']='Bearer '+os.environ[c['secretRef']]
        return httpx.AsyncClient(base_url=c['baseUrl'],headers=headers,timeout=httpx.Timeout(c['readTimeout'],connect=c['connectTimeout']),trust_env=False,follow_redirects=False,event_hooks={'request':[guard]},transport=transport or PinnedTransport(origin.hostname,addresses[0]))
    async def admit(self):
        v=self.read();c=validate(v['config'])
        if c['kind']=='openrouter':return AdmittedConnection('openrouter',c['baseUrl'],c['model'],v['revision'],connect_timeout=c['connectTimeout'],read_timeout=c['readTimeout'])
        if v.get('verification',{}).get('fingerprint')!=fingerprint(c) or v['verification'].get('state')!='verified':raise APIError(503,'저장된 모델의 도구·SSE 호환 검증이 필요합니다.')
        return AdmittedConnection(c['kind'],c['baseUrl'],c['model'],v['revision'],await self.client(c),c['connectTimeout'],c['readTimeout'])
    async def check(self,c,transport=None):
        c=validate(c)
        if c['kind']=='openrouter':raise APIError(400,'이 호환 시험은 OpenAI 호환 backend 전용입니다. OpenRouter 기본 경로는 유지됩니다.')
        # Explicit administrator action only; never called from save/readiness.
        tool={'type':'function','function':{'name':'connection_probe','description':'Return the synthetic probe token','strict':False,'parameters':{'type':'object','properties':{'probe':{'type':'string'}},'required':['probe'],'additionalProperties':False}}}
        content={'model':c['model'],'messages':[{'role':'user','content':'Synthetic compatibility test. Call connection_probe with probe=ok.'}],'tools':[tool],'tool_choice':'auto','stream':True,'max_tokens':64}
        fragments={};done=False;usage=None;finished=False
        client=await self.client(c,transport)
        try:
            async with client:
                async with asyncio.timeout(15):
                    async with client.stream('POST','chat/completions',json=content) as response:
                        if response.status_code!=200 or 'text/event-stream' not in response.headers.get('content-type',''):raise APIError(400,'모델 연결 또는 SSE 응답을 확인하지 못했습니다.')
                        size=0
                        async for line in response.aiter_lines():
                            size+=len(line)
                            if size>16384:raise APIError(400,'호환 시험 응답 크기를 초과했습니다.')
                            if line=='data: [DONE]':done=True;continue
                            if not line.startswith('data: '):continue
                            packet=json.loads(line[6:])
                            if packet.get('error'):raise APIError(400,'모델이 오류를 반환했습니다.')
                            if isinstance(packet.get('usage'),dict):usage=packet['usage']
                            for choice in packet.get('choices',[]):
                                # Narration is legal in a tool stream; it is not probe output.
                                if choice.get('finish_reason')=='tool_calls':finished=True
                                for f in choice.get('delta',{}).get('tool_calls',[]):
                                    call=fragments.setdefault(f.get('index',0),{'name':'','arguments':''});fun=f.get('function',{})
                                    for k in call:call[k]+=fun.get(k,'')
            if not done or not finished or len(fragments)!=1 or fragments[0]['name']!='connection_probe' or json.loads(fragments[0]['arguments'])!={'probe':'ok'}:raise APIError(400,'도구 호출·SSE 완료 계약이 맞지 않습니다.')
        except asyncio.CancelledError:raise
        except (httpx.HTTPError,TimeoutError,ValueError,KeyError,TypeError,AttributeError):raise APIError(400,'호환 시험이 실패했습니다. 기존 설정은 유지됩니다.') from None
        result={'state':'verified','connection':True,'tools':True,'sse':True,'fingerprint':fingerprint(c),'checkedAt':time.time(),'basis':'explicit-synthetic-tool-stream-probe','usageReported':usage is not None}
        self.checks[fingerprint(c)]=result;return result
    async def publish(self,c,expected_revision):
        if self.path is None:raise APIError(409,'설정 저장 경로가 없는 읽기 전용 배포입니다.')
        c=validate(c)
        async with self.lock:
            with publication_lock(self.path):
                return await self._publish_locked(c,expected_revision)
    async def _publish_locked(self,c,expected_revision):
            old=self.read()
            if type(expected_revision) is not int or old['revision']!=expected_revision:raise APIError(409,'다른 관리자가 설정을 변경했습니다. 다시 조회하세요.')
            proof=self.checks.get(fingerprint(c))
            if c['kind']=='openai-compatible' and (not proof or time.time()-proof['checkedAt']>300):raise APIError(409,'이 설정으로 최근 도구·SSE 호환 시험을 먼저 실행하세요.')
            value={'revision':old['revision']+1,'config':c,'verification':proof or {'state':'existing-openrouter-path'},'history':([{'config':old['config'],'verification':old.get('verification')}]+old['history'])[:5]}
            path=self.path
            if path is None:raise APIError(409,'모델 연결 설정은 읽기 전용입니다.')
            directory=path.parent
            if not directory.is_dir() or directory.is_symlink() or path.is_symlink():raise APIError(409,'기존 비공개 설정 폴더가 필요합니다.')
            fd,name=tempfile.mkstemp(dir=directory,prefix='.chat-connection-')
            try:
                with os.fdopen(fd,'w') as output:json.dump(value,output);output.flush();os.fsync(output.fileno())
                os.replace(name,path)
            except OSError:raise APIError(503,'설정 저장 실패입니다. 상태를 다시 조회하세요.') from None
            finally:
                if os.path.exists(name):os.unlink(name)
            return self.public()
    async def rollback(self,expected_revision):
        old=self.read()
        if not old['history']:raise APIError(409,'되돌릴 이전 설정이 없습니다.')
        previous=old['history'][0];proof=previous.get('verification')
        if proof and proof.get('state')=='verified':self.checks[fingerprint(previous['config'])]={**proof,'checkedAt':time.time()}
        return await self.publish(previous['config'],expected_revision)
