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
        if not isinstance(value,str):raise ValueError()
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
        return (str(p.hostname),)
    if p.scheme!='https':raise APIError(400,'원격 모델은 HTTPS가 필요합니다.')
    if not resolve:return ()
    try:addresses={str(x[4][0]) for x in socket.getaddrinfo(p.hostname,p.port or 443,type=socket.SOCK_STREAM)}
    except OSError:raise APIError(503,'모델 주소의 DNS를 확인할 수 없습니다.') from None
    if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):raise APIError(400,'사설·메타데이터·로컬 네트워크 모델 주소는 허용되지 않습니다.')
    return tuple(sorted(addresses))

def validate(value, *, available=True):
    if not isinstance(value,dict) or set(value)-FIELDS:raise APIError(400,'알 수 없는 연결 설정입니다.')
    c=copy.deepcopy(value)
    if not isinstance(c.get('kind'),str) or c['kind'] not in {'openrouter','openai-compatible'}:raise APIError(400,'지원하지 않는 연결 방식입니다.')
    c['baseUrl']=canonical_url(c.get('baseUrl',''))
    if c['kind']=='openrouter':
        if c['baseUrl']!=DEFAULT_URL or c.get('auth')!='existing-openrouter' or c.get('secretRef'):raise APIError(400,'OpenRouter는 기존 역할별 인증 경로를 사용합니다.')
        try:c['model']=normalize_model(c['model'])
        except (ValueError,KeyError):raise APIError(400,'OpenRouter 모델 ID를 확인하세요.') from None
    else:
        if c['baseUrl']==DEFAULT_URL:raise APIError(400,'공식 OpenRouter 주소는 OpenRouter 연결 방식을 사용하세요.')
        if available:destination(c['baseUrl'],resolve=False)
        if not isinstance(c.get('model'),str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:-]{0,159}',c['model']):raise APIError(400,'모델 ID를 확인하세요.')
        if not isinstance(c.get('auth'),str) or c['auth'] not in {'none','secret-ref','session-key'}:raise APIError(400,'인증 방식은 none 또는 등록된 비밀 참조입니다.')
        ref=c.get('secretRef')
        if ref is not None and not isinstance(ref,str):raise APIError(400,'비밀 참조 이름을 확인하세요.')
        ref=ref or None
        allowed={x.strip() for x in os.getenv('CHAT_LLM_SECRET_REFS','').split(',') if x.strip()}
        if c['auth']=='secret-ref' and (not ref or available and (ref not in allowed or not os.getenv(ref))):raise APIError(400,'기존 배포 환경의 등록된 비밀 참조가 필요합니다.')
        if c['auth'] in {'none','session-key'} and ref:raise APIError(400,'인증 없음에는 비밀 참조를 지정하지 않습니다.')
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
        if self.client is not None:
            try:await self.client.aclose()
            finally:self.client.headers.pop('Authorization',None)
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
        self._active=None;self._credential=None
        self.default={'kind':'openrouter','baseUrl':DEFAULT_URL,'model':os.getenv('CHAT_MODEL','openai/gpt-6-luna'),'auth':'existing-openrouter','secretRef':None,'connectTimeout':10,'readTimeout':75}
    def openrouter(self,value):
        candidates=[value['config'],value.get('openrouterConfig',{}),*[h['config'] for h in value['history']],*[p['config'] for p in value.get('profiles',[])],self.default]
        for c in candidates:
            if c.get('kind')=='openrouter':return validate(c)
        raise APIError(503,'OpenRouter 기본 설정을 확인하세요.')
    def openrouter_ready(self,value):
        try:
            self.openrouter(value)
            from integration.provider_key import ProviderKey
            ProviderKey.from_environment('chat').snapshot()
            return True
        except Exception:return False
    def require_openrouter(self,value):
        if not self.openrouter_ready(value):raise APIError(503,'OpenRouter 기본 모델과 기존 역할별 키 설정을 먼저 준비하세요.')
    async def submit_key(self,c,token,expected_revision):
        c=validate(c)
        if c['kind']!='openai-compatible' or c['auth']!='session-key':raise APIError(400,'Modal 메모리 키 연결을 선택하세요.')
        if not isinstance(token,str) or not re.fullmatch(r'wk-[A-Za-z0-9_-]{4,200}\.ws-[A-Za-z0-9_-]{4,300}',token):raise APIError(400,'추론 endpoint의 Proxy TokenID.TokenSecret만 입력하세요. 계정 관리 키나 AWS 키는 사용할 수 없습니다.')
        async with self.lock:
            old=self.read();self.same_revision(old,expected_revision)
            if self._active is not None:raise APIError(409,'새 키를 입력하려면 먼저 Modal을 끄세요.')
            self.require_openrouter(old)
            self.write({**old,'revision':old['revision']+1})
            self._credential=(fingerprint(c),token)
            return self.public()
    async def disable(self,expected_revision):
        async with self.lock:
            old=self.read();self.same_revision(old,expected_revision)
            self._active=None;self._credential=None;self.checks.clear()
            c=self.openrouter(old)
            self.write({**old,'revision':old['revision']+1,'config':c,'activeProfileId':None})
            return self.public()
    def credential_for(self,c):
        if c['auth']!='session-key':raise APIError(409,'Modal 연결은 대시보드 메모리 키 방식으로 다시 저장하세요.')
        if self._credential is None or self._credential[0]!=fingerprint(c):raise APIError(409,'이 연결 설정의 추론 키를 먼저 입력하세요. 끄기·재시작 후에는 다시 입력해야 합니다.')
        return self._credential[1]
    def read(self):
        if self.path is None or not self.path.exists():return self.with_profiles({'revision':0,'config':copy.deepcopy(self.default),'history':[],'verification':{'state':'existing-openrouter-path','connection':None,'tools':None,'sse':None}})
        try:
            if self.path.is_symlink() or self.path.stat().st_size>32000:raise ValueError()
            v=json.loads(self.path.read_text())
            if not isinstance(v,dict) or type(v['revision']) is not int or v['revision']<=0 or not isinstance(v['history'],list):raise ValueError()
            if not isinstance(v.get('verification',{}),dict):raise ValueError()
            for entry in v['history']:
                if not isinstance(entry,dict) or not isinstance(entry.get('config'),dict):raise ValueError()
                if entry.get('verification') is not None and not isinstance(entry['verification'],dict):raise ValueError()
            validate(v['config'],available=False)
            if 'openrouterConfig' in v:
                if not isinstance(v['openrouterConfig'],dict) or v['openrouterConfig'].get('kind')!='openrouter':raise ValueError()
                validate(v['openrouterConfig'],available=False)
            return self.with_profiles(v)
        except (OSError,ValueError,TypeError,KeyError,APIError):raise APIError(503,'모델 연결 설정을 읽을 수 없습니다. 기존 파일을 보존하고 확인하세요.') from None
    def with_profiles(self,value):
        # Older files stay readable; materialize names only on the next explicit save.
        if 'profiles' not in value:
            value['profiles']=[{'id':'openrouter','name':'OpenRouter','config':copy.deepcopy(self.default),'verification':{'state':'existing-openrouter-path'}}]
            value['activeProfileId']='openrouter'
            if fingerprint(value['config'])!=fingerprint(self.default):
                value['profiles'].append({'id':'legacy-active','name':'기존 연결','config':copy.deepcopy(value['config']),'verification':copy.deepcopy(value.get('verification',{}))})
                value['activeProfileId']='legacy-active'
        profiles=value['profiles']
        if not isinstance(profiles,list) or len(profiles)>10:raise ValueError('profiles')
        ids=set();names=set()
        for profile in profiles:
            if not isinstance(profile,dict):raise ValueError('profile')
            self.profile_identity(profile.get('id'),profile.get('name'))
            if profile['id'] in ids or profile['name'] in names:raise ValueError('duplicate profile')
            ids.add(profile['id']);names.add(profile['name'])
            validate(profile['config'],available=False)
            if not isinstance(profile.get('verification',{}),dict):raise ValueError('profile proof')
        if value.get('activeProfileId') is not None and value['activeProfileId'] not in ids:raise ValueError('active profile')
        return value
    @staticmethod
    def profile_identity(profile_id,name):
        if not isinstance(profile_id,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}',profile_id):raise APIError(400,'프로필 식별자를 확인하세요.')
        if not isinstance(name,str) or not name.strip() or name!=name.strip() or len(name)>60:raise APIError(400,'프로필 이름은 공백 없이 시작·끝나는 1–60자입니다.')
    @staticmethod
    def same_revision(old,expected):
        if type(expected) is not int or old['revision']!=expected:raise APIError(409,'다른 관리자가 설정을 변경했습니다. 다시 조회하세요.')
    async def save_profile(self,profile_id,name,c,expected_revision):
        if self.path is None:raise APIError(409,'설정 저장 경로가 없는 읽기 전용 배포입니다.')
        self.profile_identity(profile_id,name);c=validate(c)
        async with self.lock:
            with publication_lock(self.path):
                value=self.read();self.same_revision(value,expected_revision)
                previous=next((p for p in value['profiles'] if p['id']==profile_id),None)
                if any(p['name']==name and p['id']!=profile_id for p in value['profiles']):raise APIError(409,'같은 이름의 프로필이 있습니다.')
                if previous is None and len(value['profiles'])>=10:raise APIError(409,'프로필은 최대 10개입니다. 기존 프로필을 편집하세요.')
                proof=self.checks.get(fingerprint(c))
                if not proof or time.time()-proof['checkedAt']>300:proof=None
                if previous and fingerprint(previous['config'])==fingerprint(c):proof=proof or previous.get('verification')
                if c['kind']=='openrouter':proof={'state':'existing-openrouter-path'}
                profile={'id':profile_id,'name':name,'config':c,'verification':proof or {'state':'needs-check'}}
                value['profiles']=[profile if p['id']==profile_id else p for p in value['profiles']]
                if previous is None:value['profiles'].append(profile)
                # Editing a saved profile never changes the active connection.
                if value.get('activeProfileId')==profile_id and fingerprint(c)!=fingerprint(value['config']):value['activeProfileId']=None
                value['revision']+=1;self.write(value)
                return self.public()
    async def apply_profile(self,profile_id,expected_revision):
        if self.path is None:raise APIError(409,'설정 저장 경로가 없는 읽기 전용 배포입니다.')
        async with self.lock:
            with publication_lock(self.path):
                old=self.read();self.same_revision(old,expected_revision)
                profile=next((p for p in old['profiles'] if p['id']==profile_id),None)
                if profile is None:raise APIError(404,'저장된 프로필을 찾을 수 없습니다.')
                c=validate(profile['config']);proof=profile.get('verification',{})
                if c['kind']=='openai-compatible':self.credential_for(c)
                if self._active and old.get('activeProfileId')==profile_id and fingerprint(c)==fingerprint(self._active):return self.public()
                return await self._publish_locked(c,expected_revision,profile=profile)
    def public(self):
        value=self.read();c=self._active or self.openrouter(value)
        active_id=next((p['id'] for p in value['profiles'] if p['id']==value.get('activeProfileId') and fingerprint(p['config'])==fingerprint(c)),None)
        if active_id is None and self._active is None:active_id=next((p['id'] for p in value['profiles'] if fingerprint(p['config'])==fingerprint(c)),None)
        return {**value,'config':copy.deepcopy(c),'activeProfileId':active_id,'defaultConfig':copy.deepcopy(self.openrouter(value)),'writable':self.path is not None,'allowedBaseUrls':sorted({DEFAULT_URL,*[s.strip() for s in os.getenv('CHAT_LLM_ALLOWED_BASE_URLS','').split(',') if s.strip()]}),'secretReferences':[],'modal':{'active':self._active is not None,'keyPresent':self._credential is not None,'readyProfiles':[p['id'] for p in value['profiles'] if self._credential and self._credential[0]==fingerprint(p['config'])],'openrouterReady':self.openrouter_ready(value)},'scope':'chat-only; single instance/worker; profiler unchanged','verificationNotice':'과거 호환 확인은 현재 가용성을 보장하지 않습니다.'}
    async def client(self,c,transport=None,*,token=None):
        try:addresses=await asyncio.wait_for(asyncio.to_thread(destination,c['baseUrl']),c['connectTimeout'])
        except TimeoutError:raise APIError(503,'모델 DNS 확인 시간이 초과됐습니다.') from None
        origin=urlsplit(c['baseUrl']);path=origin.path+'/chat/completions'
        async def guard(request):
            if request.method!='POST' or request.url.scheme!=origin.scheme or request.url.host!=origin.hostname or (request.url.port or (443 if request.url.scheme=='https' else 80))!=(origin.port or (443 if origin.scheme=='https' else 80)) or request.url.path!=path or request.url.query or request.url.fragment:raise APIError(503,'허용된 모델 completion 경로만 사용할 수 있습니다.')
            await asyncio.to_thread(destination,c['baseUrl'],resolve=False)
        headers={}
        if c['auth']=='session-key':headers['Authorization']='Bearer '+(token if token is not None else self.credential_for(c))
        elif c['auth']=='secret-ref':headers['Authorization']='Bearer '+os.environ[c['secretRef']]
        return httpx.AsyncClient(base_url=c['baseUrl'],headers=headers,timeout=httpx.Timeout(c['readTimeout'],connect=c['connectTimeout']),trust_env=False,follow_redirects=False,event_hooks={'request':[guard]},transport=transport or PinnedTransport(origin.hostname,addresses[0]))
    async def admit(self):
        # Capture before the first await: already admitted calls retain their provider.
        v=self.read();c=copy.deepcopy(self._active or self.openrouter(v))
        if c['kind']=='openrouter':
            self.require_openrouter(v)
            return AdmittedConnection('openrouter',c['baseUrl'],c['model'],v['revision'],connect_timeout=c['connectTimeout'],read_timeout=c['readTimeout'])
        token=self.credential_for(c)
        return AdmittedConnection(c['kind'],c['baseUrl'],c['model'],v['revision'],await self.client(c,token=token),c['connectTimeout'],c['readTimeout'])
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
    async def _publish_locked(self,c,expected_revision,*,profile=None):
        old=self.read()
        self.same_revision(old,expected_revision)
        proof=profile.get('verification') if profile is not None else self.checks.get(fingerprint(c))
        self.require_openrouter(old)
        if c['kind']=='openai-compatible':self.credential_for(c)
        value={'revision':old['revision']+1,'openrouterConfig':copy.deepcopy(c if c['kind']=='openrouter' else self.openrouter(old)),'config':c,'verification':proof or {'state':'existing-openrouter-path'},'history':([{'config':old['config'],'verification':old.get('verification')}]+old['history'])[:5],'profiles':old['profiles'],'activeProfileId':profile['id'] if profile is not None else None}
        self.write(value)
        self._active=copy.deepcopy(c) if c['kind']=='openai-compatible' else None
        if c['kind']=='openrouter':self._credential=None
        return self.public()
    def write(self,value):
        wire=json.dumps(value,ensure_ascii=False)
        if len(wire.encode())>32000:raise APIError(409,'설정 저장 크기를 초과했습니다. 프로필 내용을 줄이세요.')
        path=self.path
        if path is None:raise APIError(409,'모델 연결 설정은 읽기 전용입니다.')
        directory=path.parent
        if not directory.is_dir() or directory.is_symlink() or path.is_symlink():raise APIError(409,'기존 비공개 설정 폴더가 필요합니다.')
        fd,name=tempfile.mkstemp(dir=directory,prefix='.chat-connection-')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as output:output.write(wire);output.flush();os.fsync(output.fileno())
            os.replace(name,path)
        except OSError:raise APIError(503,'설정 저장 실패입니다. 상태를 다시 조회하세요.') from None
        finally:
            if os.path.exists(name):os.unlink(name)
    async def rollback(self,expected_revision):
        old=self.read()
        if not old['history']:raise APIError(409,'되돌릴 이전 설정이 없습니다.')
        previous=old['history'][0];proof=previous.get('verification')
        if previous['config']['kind']!='openrouter':raise APIError(409,'Modal은 이전 설정 복구로 활성화하지 않습니다. 연결을 선택하고 키 입력 후 명시적으로 적용하세요.')
        if proof and proof.get('state')=='verified':self.checks[fingerprint(previous['config'])]={**proof,'rollbackAt':time.time()}
        return await self.publish(previous['config'],expected_revision)
