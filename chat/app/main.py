import asyncio,json,os,secrets,time,uuid
from collections import defaultdict,deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated,Literal
from urllib.parse import urlsplit
import httpx
from psycopg import OperationalError as DatabaseUnavailable
from fastapi import FastAPI,Request,Response,Depends,Path as ApiPath,Query,WebSocket,Body
from fastapi.responses import FileResponse,StreamingResponse,JSONResponse,HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer,HTTPAuthorizationCredentials
from itsdangerous import URLSafeTimedSerializer,BadSignature,SignatureExpired
from pydantic import BaseModel,Field,UUID4
from app.errors import APIError,install_error_handlers
from app.models import Conversation,PutConversation,PutTurn,SuccessResponse,Turn,StrictModel,ErrorResponse
from app.remote_store import RemoteStore
from app.bridge import BridgeStore
from app.search_client import SearchClient
from app.runtime import Runtime
from app.resource_limits import limits_for, ManagedStreamingResponse, notify_turn
from app.agent import AgentRuntime
from app.low_latency import LowLatencyRuntime
from app.card_contract import readable_conversation, readable_turn, readable_replay

BASE='/v1/chat/conversations';TEST_BASE='/v1/chat/test/conversations';STATIC=Path(__file__).parent/'static'
TOKEN=os.getenv('AI_SERVICE_TOKEN','');OWNER=str(uuid.uuid4())
def current_service_token():
 from integration.service_token import shared_token_configured
 if shared_token_configured() or os.getenv('CHAT_BE_SERVICE_TOKEN_FILE'):
  from integration.service_token import expected_token
  from integration.provider_key import KeyUnavailable
  try:return expected_token('chat')
  except KeyUnavailable:raise APIError(503,'서비스 인증이 아직 설정되지 않았습니다.') from None
 return TOKEN

def service_header_valid(value):
 try:
  expected=current_service_token()
  return bool(expected) and valid_token(value,'Bearer '+expected)
 except APIError:return False

class LocalServiceAuth(httpx.Auth):
 def auth_flow(self,request):
  request.headers['Authorization']='Bearer '+current_service_token()
  yield request
 async def async_auth_flow(self,request):
  request.headers['Authorization']='Bearer '+await asyncio.to_thread(current_service_token)
  yield request
LEASE_RELEASE_TIMEOUT_SECONDS=5
from app.security import SIGNER,SECURE,same_origin,throttle,valid_token,has_demo_access,require_demo_access,demo_access_version,DEMO_ACCESS_SECONDS
from app.admin import router as admin_router

from app.release import revision as app_revision

def runtime_info():
 mode=getattr(app.state,'runtime_mode','live')
 result={'mode':mode,'model':app.state.runtime.model,'agent':'mock-v1' if mode=='mock' else getattr(app.state.runtime,'agent_name',os.getenv('CHAT_AGENT_MODE','baseline'))}
 if mode=='mock':
  from app.mock_runtime import SCENARIOS
  result['scenarios']=SCENARIOS
 elif getattr(app.state.runtime,'model_config',None):result.update(app.state.runtime.model_config.public())
 return result

async def provider_key_request(request):
 # This send boundary keeps tracing without creating a local budget reservation.
 url=request.url
 official=(url.scheme=='https' and url.host=='openrouter.ai' and url.port in (None,443)
           and not (url.username or url.password or url.query or url.fragment))
 metadata=request.method=='GET' and (url.path.startswith('/api/v1/model/') or
           url.path.startswith('/api/v1/models/') and url.path.endswith('/endpoints'))
 completion=request.method=='POST' and url.path=='/api/v1/chat/completions'
 if not official or not (metadata or completion):raise APIError(503,'허용된 OpenRouter 요청만 전송할 수 있습니다.')
 if completion:
  from app.model_trace import attempted
  attempted(json.loads(request.content),None)

@asynccontextmanager
async def lifespan(app):
 if not os.getenv('DEMO_ACCESS_TOKEN'):raise ValueError('DEMO_ACCESS_TOKEN is required')
 app.state.runtime_mode=os.getenv('CHAT_RUNTIME_MODE','live')
 if app.state.runtime_mode not in {'live','mock'}:raise ValueError('CHAT_RUNTIME_MODE must be live or mock')
 app.state.budget_mode=os.getenv('AI_BUDGET_MODE','').strip() or 'provider-key-limits'
 if app.state.budget_mode not in {'guarded','provider-key-limits'}:raise ValueError('AI_BUDGET_MODE must be guarded or provider-key-limits')
 mode=os.getenv('STORE_MODE','http');bridge_mode=mode=='bridge'
 if mode=='postgres':
  from app.local_store import LocalStore
  app.state.store=LocalStore(os.environ['DATABASE_URL'])
 elif mode=='bridge':app.state.store=BridgeStore(os.environ['STORE_TOKEN'])
 elif mode=='http':app.state.store=RemoteStore(os.environ['STORE_URL'],os.environ['STORE_TOKEN'])
 else:raise ValueError('Unknown STORE_MODE')
 if app.state.runtime_mode=='mock':
  from app.mock_runtime import MockRuntime,NoExternalSearch
  app.state.search=NoExternalSearch();runtime_type=MockRuntime
 else:
  app.state.search=SearchClient(os.getenv('SEARCH_URL','https://wjo-ai-search-demo.hf.space'),token=os.environ['SEARCH_TOKEN'],source='chat',timeout=45)
  runtime_type=LowLatencyRuntime
 app.state.runtime=runtime_type(app.state.store,app.state.search,OWNER)
 app.state.budget_store=None
 app.state.budget_account=None
 bridge_budget=False
 if app.state.runtime_mode=='live' and app.state.budget_mode=='guarded':
  from app.task_budget import BudgetHTTP,budget_settings,validate_limit
  account,expected_limit=budget_settings()
  app.state.budget_account=account
  budget_store=app.state.store
  if os.getenv('BUDGET_STORE_URL'):
   from app.budget_ledger import BudgetLedger
   budget_store=BudgetLedger(os.environ['BUDGET_STORE_URL'],os.environ['STORE_TOKEN'])
   app.state.budget_store=budget_store
  # A bridge attaches after lifespan yields; keep it unready until its ledger responds.
  bridge_budget=bridge_mode and app.state.budget_store is None
  if not bridge_budget:validate_limit(await budget_store.call('budget_summary',account=account),expected_limit)
  app.state.runtime.http=BudgetHTTP(app.state.runtime.http,budget_store,account=account,expected_limit=expected_limit,check_summary=bridge_budget)
  app.state.runtime.catalog.http=app.state.runtime.http
 elif app.state.runtime_mode=='live':
  app.state.runtime.http.follow_redirects=False
  app.state.runtime.http.event_hooks['request'].append(provider_key_request)
 from app.protocol_fixture import ProtocolFixture
 app.state.fixture=ProtocolFixture(app.state.runtime)
 app.state.runtime.healthy=False
 app.state.proxy=httpx.AsyncClient(base_url=os.getenv('INTERNAL_URL','http://127.0.0.1:7860'),auth=LocalServiceAuth(),timeout=120)
 async def heartbeat():
  epoch=None;owned=False;pending=None
  while True:
   try:
    runtime=app.state.runtime
    if bridge_budget:validate_limit(await budget_store.call('budget_summary',account=account),expected_limit)
    current=app.state.store.epoch if bridge_mode else 0
    reconnect=current!=epoch or not runtime.healthy or runtime.recovery_version!=runtime.recovered_version
    if reconnect:
     runtime.healthy=False
     tasks=list(runtime.jobs.values())
     for task in tasks:task.cancel()
     await asyncio.gather(*tasks,return_exceptions=True)
     if pending is None:
      if owned:expected,claim=runtime.owner,False
      else:
       observed=await app.state.store.call('lease',owner=runtime.owner,inspect=True)
       if observed['active']:
        await asyncio.sleep(2);continue
       expected,claim=observed['owner'],True
      pending={'owner':expected,'next_owner':str(uuid.uuid4()),'claim':claim,'version':runtime.recovery_version}
    if pending is not None:
     ok=await app.state.store.call('lease',owner=pending['owner'],next_owner=pending['next_owner'],claim=pending['claim'],recover=True)
     if ok:
      runtime.owner=pending['next_owner'];runtime.recovered_version=pending['version'];owned=True;epoch=current
     else:owned=False
     pending=None
    else:
     ok=await app.state.store.call('lease',owner=runtime.owner,next_owner=runtime.owner)
     if not ok:owned=False
    runtime.healthy=bool(ok) and runtime.recovery_version==runtime.recovered_version
   except Exception:app.state.runtime.healthy=False
   await asyncio.sleep(8 if app.state.runtime.healthy else 2)
 heartbeat_task=asyncio.create_task(heartbeat())
 try:yield
 finally:
  heartbeat_task.cancel();await asyncio.gather(heartbeat_task,return_exceptions=True)
  await app.state.runtime.close()
  if app.state.budget_store is not None:await app.state.budget_store.close()
  await app.state.proxy.aclose();await app.state.search.close()
  try:
   await asyncio.wait_for(app.state.store.call('lease',owner=app.state.runtime.owner,next_owner=app.state.runtime.owner,release=True),LEASE_RELEASE_TIMEOUT_SECONDS)
  except Exception:pass
  finally:await app.state.store.close()
app=FastAPI(title='선잘알 V2 Chat API',version='1.5.2',lifespan=lifespan,
 responses={code:{'model':ErrorResponse} for code in [400,401,404,409,413,429,500,503]})
install_error_handlers(app)
@app.websocket('/internal/store-bridge')
async def store_bridge(socket:WebSocket):
 if not isinstance(app.state.store,BridgeStore):await socket.close(code=1008);return
 before=app.state.store.epoch
 await app.state.store.attach(socket)
 if app.state.store.epoch!=before and app.state.store.socket is None:app.state.runtime.healthy=False

from app.demo_api import router as demo_api_router
app.include_router(demo_api_router)
app.mount('/static',StaticFiles(directory=STATIC),name='static')
if os.getenv('CHAT_MEDIA_ROOT'):
 app.mount('/media',StaticFiles(directory=os.environ['CHAT_MEDIA_ROOT']),name='product-media')
bearer=HTTPBearer(auto_error=False)
async def service(credentials:Annotated[HTTPAuthorizationCredentials|None,Depends(bearer)]):
 expected=await asyncio.to_thread(current_service_token)
 if credentials is None or not expected or not valid_token(credentials.credentials,expected):raise APIError(401,'서비스 인증에 실패했습니다.')
Service=Annotated[None,Depends(service)]
async def demo_session(request:Request):
 same_origin(request);require_demo_access(request)
 try:sid=SIGNER.loads(request.cookies.get('chat_session',''),salt='demo',max_age=86400*30)
 except (BadSignature,SignatureExpired):raise APIError(401,'체험 세션을 시작해 주세요.')
 sender=await app.state.store.call('session',sid=sid)
 return sender
Demo=Annotated[int,Depends(demo_session)]
@app.middleware('http')
async def headers(request,call_next):
 from app.errors import error_body
 if request.method in {'POST','PUT'} and int(request.headers.get('content-length','0'))>20000:
  response=JSONResponse(error_body('요청이 너무 큽니다.',413),status_code=413)
 elif (request.url.path in {'/docs','/redoc','/openapi.json','/docs/oauth2-redirect','/guide/maintenance.md'} or (request.url.path.startswith('/guide/') and request.url.path not in {'/guide/integration.md','/guide/testing.md','/guide/backend-client.py'})) and not has_demo_access(request) and not await asyncio.to_thread(service_header_valid, request.headers.get('authorization','')):
  response=JSONResponse(error_body('체험 토큰 또는 서비스 토큰이 필요합니다.',401),status_code=401)
 else:response=await call_next(request)
 response.headers['Cache-Control']='no-store, no-transform'
 response.headers['X-Content-Type-Options']='nosniff'
 response.headers['Referrer-Policy']='same-origin'
 response.headers['X-Demo-Revision']=app_revision()
 response.headers['X-App-Revision']=app_revision()
 response.headers['X-Chat-Mode']=getattr(app.state,'runtime_mode','live')
 response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'self' https://huggingface.co"
 if request.url.path in {'/docs','/redoc'}:del response.headers['Content-Security-Policy']
 return response
@app.get('/',include_in_schema=False)
def index():
 title='고정 응답 테스트' if getattr(app.state,'runtime_mode','live')=='mock' else '채팅 테스트'
 return HTMLResponse((STATIC/'index.html').read_text().replace('__CHAT_TITLE__',title))
@app.get('/guide',include_in_schema=False)
def guide():return FileResponse(STATIC/('mock-guide.html' if getattr(app.state,'runtime_mode','live')=='mock' else 'guide.html'))
@app.get('/guide/maintenance.md',include_in_schema=False)
def maintenance_guide():return FileResponse(Path(__file__).parent.parent/'MAINTENANCE.md',media_type='text/markdown')

@app.get('/guide/integration.md',include_in_schema=False)
def integration_doc():return FileResponse(Path(__file__).parent.parent/'INTEGRATION.md',media_type='text/markdown; charset=utf-8',filename='AI-backend-chat-integration.md')
@app.get('/guide/testing.md',include_in_schema=False)
def testing_doc():return FileResponse(Path(__file__).parent.parent/'TESTING.md',media_type='text/markdown; charset=utf-8')
@app.get('/guide/backend-client.py',include_in_schema=False)
def integration_client():return FileResponse(Path(__file__).with_name('demo_integration_client.py'),media_type='text/x-python',filename='backend_client.py')
@app.get('/healthz')
def live():return {'alive':True,'revision':app_revision(),'mode':getattr(app.state,'runtime_mode','live')}
@app.get('/readyz',dependencies=[Depends(service)])
async def ready():
 if not app.state.runtime.healthy:raise APIError(503,'실행 서버가 준비되지 않았습니다.')
 if hasattr(app.state.runtime,'provider_key') and not (await asyncio.to_thread(app.state.runtime.provider_key.status))['configured']:raise APIError(503,'LLM 키가 설정되지 않았거나 읽을 수 없습니다.')
 try:
  async with asyncio.timeout(5):await app.state.store.call('health')
 except (APIError,httpx.HTTPError,TimeoutError,DatabaseUnavailable):raise APIError(503,'대화 저장소 연결을 복구하고 있습니다.',headers={'Retry-After':'5'}) from None
 try:
  async with asyncio.timeout(5):await app.state.search.ready()
 except (httpx.HTTPError,ValueError,TimeoutError):raise APIError(503,'검색 서버 연결을 복구하고 있습니다.',headers={'Retry-After':'5'}) from None
 result={'ready':True,'database':'postgresql','budgetMode':app.state.budget_mode,**runtime_info()}
 if app.state.budget_store is not None:
  result['sharedBudget']=await app.state.budget_store.call('budget_summary',account=app.state.budget_account)
  result['budgetAccount']=app.state.budget_account
 return result
@app.get('/internal/credentials',dependencies=[Depends(service)])
async def credential_status():
 from integration.provider_key import ProviderKey
 from integration.service_token import ServiceToken
 provider=getattr(app.state.runtime,'provider_key',None) or ProviderKey.from_environment('chat')
 key,token=await asyncio.gather(asyncio.to_thread(provider.runtime_status),asyncio.to_thread(ServiceToken.from_environment('chat').status))
 token['usage']={'state':'not-collected','lastSuccessfulCallAt':None,'scope':'responding-process'}
 return {'service':'chat','environment':provider.environment,'provider':key,'backend':token}
@app.put(TEST_BASE+'/{conversationId}',response_model=SuccessResponse[Conversation],responses={201:{'model':SuccessResponse[Conversation]}})
@app.put(BASE+'/{conversationId}',response_model=SuccessResponse[Conversation],responses={201:{'model':SuccessResponse[Conversation]}})
async def conversation(conversationId:UUID4,body:PutConversation,response:Response,request:Request,auth:Service):
 data,made=await app.state.store.call('conversation',cid=str(conversationId),sender=body.sender_id,recipient=body.recipient_id,**({'protocol_fixture':True} if request.url.path.startswith(TEST_BASE) else {}))
 response.status_code=201 if made else 200
 return {'message':'대화를 조회했습니다.','data':readable_conversation(data)}
async def selected_runtime(cid,request):
 binding=await app.state.store.call('binding',cid=cid)
 fixture=request.url.path.startswith(TEST_BASE)
 if bool(binding.get('protocolFixture'))!=fixture:raise APIError(409,'이 대화의 실행 경로를 사용해 주세요.')
 return app.state.fixture if fixture else app.state.runtime

@app.get(TEST_BASE+'/{conversationId}',response_model=SuccessResponse[Conversation])
@app.get(BASE+'/{conversationId}',response_model=SuccessResponse[Conversation])
async def service_snapshot(conversationId:UUID4,request:Request,auth:Service):
 cid=str(conversationId);await selected_runtime(cid,request)
 return {'message':'대화를 조회했습니다.','data':readable_conversation(await app.state.store.call('read',cid=cid))}

@app.get(TEST_BASE+'/{conversationId}/turns/{turnId}',response_model=SuccessResponse[Turn])
@app.get(BASE+'/{conversationId}/turns/{turnId}',response_model=SuccessResponse[Turn])
async def turn_snapshot(conversationId:UUID4,turnId:UUID4,request:Request,auth:Service):
 cid,tid=str(conversationId),str(turnId);await selected_runtime(cid,request)
 _,state=await app.state.store.call('replay',cid=cid,tid=tid,after=0)
 return {'message':'질문 상태를 조회했습니다.','data':readable_turn(state)}

@app.put(TEST_BASE+'/{conversationId}/turns/{turnId}',response_model=SuccessResponse[Turn],responses={202:{'model':SuccessResponse[Turn]}})
@app.put(BASE+'/{conversationId}/turns/{turnId}',response_model=SuccessResponse[Turn],responses={202:{'model':SuccessResponse[Turn]}})
async def turn(conversationId:UUID4,turnId:UUID4,body:PutTurn,response:Response,request:Request,auth:Service):
 cid,tid=str(conversationId),str(turnId)
 runtime=await selected_runtime(cid,request)
 quick_check=request.headers.get('x-console-quick-check')
 if (quick_check is not None or body.quick_check) and (quick_check!='true' or body.quick_check is not True or getattr(app.state,'runtime_mode','live')!='live' or runtime is getattr(app.state,'fixture',None)):
  raise APIError(400,'QUICK_CHECK_LIVE_RUNTIME_REQUIRED')
 if body.model is not None and (getattr(app.state,'runtime_mode','live')=='mock' or runtime is getattr(app.state,'fixture',None)):raise APIError(400,'고정 응답 서버에서는 LLM 모델을 선택하지 않습니다.')
 if body.prepared_context is not None and runtime is getattr(app.state,'fixture',None):raise APIError(400,'고정 응답에서는 실제 후보 준비를 사용하지 않습니다.')
 data=await runtime.accept(cid,tid,body.message,**({'model':body.model} if body.model else {}),**({'recipient_context':body.recipient_context.model_dump(by_alias=True)} if body.recipient_context is not None else {}),**({'prepared_context':body.prepared_context.model_dump(by_alias=True)} if body.prepared_context is not None else {}),**({'quick_check':True} if quick_check else {}))
 response.status_code=202 if data['status']=='PROCESSING' else 200
 return {'message':'질문 상태를 조회했습니다.','data':readable_turn(data)}
@app.get(TEST_BASE+'/{conversationId}/turns/{turnId}/events',response_class=StreamingResponse,responses={200:{'content':{'text/event-stream':{'schema':{'type':'string'}}}}})
@app.get(BASE+'/{conversationId}/turns/{turnId}/events',response_class=StreamingResponse,responses={200:{'content':{'text/event-stream':{'schema':{'type':'string'}}}}})
async def events(conversationId:UUID4,turnId:UUID4,request:Request,auth:Service,afterEventId:Annotated[int,Query(ge=0)]=0):
 if set(request.query_params)-{'afterEventId'}:raise APIError(400,'지원하지 않는 Query 필드입니다.')
 cid,tid=str(conversationId),str(turnId)
 runtime=app.state.runtime
 lease=limits_for(runtime).acquire_stream(runtime,tid)
 try:
  await selected_runtime(cid,request)
  initial=readable_replay(*await app.state.store.call('replay',cid=cid,tid=tid,after=afterEventId))
 except BaseException:
  lease.release()
  raise
 async def stream():
  cursor=afterEventId;batch,state=initial;heartbeat=time.monotonic()
  signal=runtime.signals.setdefault(tid,asyncio.Event())
  try:
   while True:
    if await request.is_disconnected():return
    for event in batch:
     cursor=event['id'];yield f"id: {cursor}\nevent: {event['event']}\ndata: {json.dumps(event['data'],ensure_ascii=False)}\n\n"
    if state['status']!='PROCESSING' and cursor>=state['lastEventId']:return
    if time.monotonic()-heartbeat>=10:yield ': keep-alive\n\n';heartbeat=time.monotonic()
    # Clear before the read so a commit during either read or yield stays visible.
    signal.clear()
    batch,state=readable_replay(*await app.state.store.call('replay',cid=cid,tid=tid,after=cursor))
    if batch or state['status']!='PROCESSING':continue
    try:await asyncio.wait_for(signal.wait(),timeout=2)
    except TimeoutError:pass
  finally:lease.release()
 return ManagedStreamingResponse(stream(),lease,media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})
@app.put(TEST_BASE+'/{conversationId}/turns/{turnId}/cancellation',response_model=SuccessResponse[Turn])
@app.put(BASE+'/{conversationId}/turns/{turnId}/cancellation',response_model=SuccessResponse[Turn])
async def cancel(conversationId:UUID4,turnId:UUID4,request:Request,auth:Service):
 cid,tid=str(conversationId),str(turnId)
 await selected_runtime(cid,request)
 await app.state.store.call('emit',cid=cid,tid=tid,kind='turn.finished',payload={'status':'CANCELLED'},owner=app.state.runtime.owner)
 app.state.runtime.cancel_owned(tid)
 notify_turn(app.state.runtime,tid)
 _,state=await app.state.store.call('replay',cid=cid,tid=tid,after=0)
 return {'message':'생성 상태를 조회했습니다.','data':readable_turn(state)}
class DemoLogin(BaseModel):
 token:str=Field(default='',max_length=2048)
@app.get('/demo/session',include_in_schema=False)
async def session_status(sender:Demo):
 return {'ready':True,'mode':getattr(app.state,'runtime_mode','live'),'senderUserId':sender}
@app.post('/demo/session',include_in_schema=False)
async def session(request:Request,response:Response,body:DemoLogin|None=None):
 same_origin(request)
 if not valid_token(body.token if body else '',os.environ['DEMO_ACCESS_TOKEN']):
  throttle(('demo-login',request.client.host),10,300)
  raise APIError(401,'체험 토큰을 확인해 주세요.')
 try:
  sid=SIGNER.loads(request.cookies.get('chat_session',''),salt='demo',max_age=86400*30)
  sender=await app.state.store.call('session',sid=sid)
 except (BadSignature,SignatureExpired):sid=None
 except APIError as exc:
  if exc.status!=401:raise
  sid=None
 if sid is None:
  throttle(('sessions',request.client.host),10,3600)
  sid=str(uuid.uuid4());sender=secrets.randbelow(9_007_199_254_740_990)+1
  await app.state.store.call('session',sid=sid,sender=sender)
 response.set_cookie('chat_session',SIGNER.dumps(sid,salt='demo'),max_age=86400*30,secure=SECURE,httponly=True,samesite='lax')
 response.set_cookie('chat_access',SIGNER.dumps({'version':demo_access_version()},salt='demo-access'),max_age=DEMO_ACCESS_SECONDS,secure=SECURE,httponly=True,samesite='lax')
 return {'ready':True,'mode':getattr(app.state,'runtime_mode','live'),'senderUserId':sender}
@app.post('/demo/logout',include_in_schema=False)
async def logout_demo(request:Request,response:Response):
 same_origin(request)
 response.delete_cookie('chat_access',secure=SECURE,httponly=True,samesite='lax')
 return {'ok':True}
@app.get('/demo/recipients',include_in_schema=False)
async def recipients(sender:Demo):
 from app.demo_profiles import demo_profiles
 return {'recipients':[] if getattr(app.state,'runtime_mode','live')=='mock' else demo_profiles()}
class DemoModel(StrictModel):
 model:str=Field(max_length=160)
@app.get('/demo/test-scenarios',include_in_schema=False)
async def test_scenarios(sender:Demo):
 from app.mock_runtime import SCENARIOS
 return {'scenarios':SCENARIOS}
@app.post('/demo/model',include_in_schema=False)
async def demo_model(body:DemoModel,sender:Demo):
 raise APIError(403,'모델과 추론 설정은 AI 서버에서 관리합니다.')
@app.get('/demo/conversations',include_in_schema=False)
async def lists(sender:Demo):return await app.state.store.call('list_conversations',sender=sender)
@app.get('/demo/conversations/{cid}',include_in_schema=False)
async def read_demo_conversation(cid:UUID4,sender:Demo):
 await app.state.store.call('owned',cid=str(cid),sender=sender)
 data,binding=await asyncio.gather(app.state.store.call('read',cid=str(cid)),app.state.store.call('binding',cid=str(cid)))
 return {'data':data,'recipient':binding.get('demoProfile'),'protocolFixture':bool(binding.get('protocolFixture'))}
async def proxied(path,body=None,stream=False):
 request=app.state.proxy.build_request('GET' if stream else 'PUT',path,json=body)
 result=await app.state.proxy.send(request,stream=stream)
 retry_headers={'Retry-After':result.headers['Retry-After']} if 'Retry-After' in result.headers else {}
 if not stream:return Response(result.content,status_code=result.status_code,media_type='application/json',headers=retry_headers)
 if result.status_code!=200:
  content=await result.aread();await result.aclose();return Response(content,status_code=result.status_code,media_type='application/json',headers=retry_headers)
 async def iterator():
  try:
   async for chunk in result.aiter_bytes():yield chunk
  finally:await result.aclose()
 return StreamingResponse(iterator(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})
class DemoConversation(StrictModel):
 protocol_fixture:bool=Field(default=False,strict=True)
 recipient_key:str=Field(default='none',alias='recipientKey',max_length=40)
@app.put('/demo/conversations/{cid}',include_in_schema=False)
async def demo_conversation(cid:UUID4,sender:Demo,response:Response,body:DemoConversation|None=Body(default=None)):
 from app.demo_profiles import demo_profile
 fixture=body.protocol_fixture if body else False
 if fixture and body.recipient_key!='none':raise APIError(400,'무료 연동 테스트에서는 수신자를 선택하지 않습니다.')
 profile=demo_profile(body.recipient_key if body else 'none')
 data,made=await app.state.store.call('conversation',cid=str(cid),sender=sender,recipient=profile['data']['recipientUserId'] if profile else 1,demo_profile=profile,**({'protocol_fixture':True} if fixture else {}))
 response.status_code=201 if made else 200
 binding=await app.state.store.call('binding',cid=str(cid))
 return {'data':data,'recipient':binding.get('demoProfile'),'protocolFixture':bool(binding.get('protocolFixture'))}
@app.put('/demo/conversations/{cid}/turns/{tid}',include_in_schema=False)
async def demo_turn(cid:UUID4,tid:UUID4,body:PutTurn,sender:Demo):
 await app.state.store.call('owned',cid=str(cid),sender=sender)
 throttle(('turns',sender),30,3600)
 binding=await app.state.store.call('binding',cid=str(cid));base=TEST_BASE if binding.get('protocolFixture') else BASE
 return await proxied(f'{base}/{cid}/turns/{tid}',body.model_dump(exclude_none=True,exclude_defaults=True))
@app.get('/demo/conversations/{cid}/turns/{tid}/events',include_in_schema=False)
async def demo_events(cid:UUID4,tid:UUID4,sender:Demo,afterEventId:Annotated[int,Query(ge=0)]=0):
 await app.state.store.call('owned',cid=str(cid),sender=sender)
 binding=await app.state.store.call('binding',cid=str(cid));base=TEST_BASE if binding.get('protocolFixture') else BASE
 return await proxied(f'{base}/{cid}/turns/{tid}/events?afterEventId={afterEventId}',stream=True)
@app.put('/demo/conversations/{cid}/turns/{tid}/cancellation',include_in_schema=False)
async def demo_cancel(cid:UUID4,tid:UUID4,sender:Demo):
 await app.state.store.call('owned',cid=str(cid),sender=sender)
 binding=await app.state.store.call('binding',cid=str(cid));base=TEST_BASE if binding.get('protocolFixture') else BASE
 return await proxied(f'{base}/{cid}/turns/{tid}/cancellation')
@app.get('/demo/conversations/{cid}/turns/{tid}/products',include_in_schema=False)
async def demo_products(cid:UUID4,tid:UUID4,sender:Demo):
 await app.state.store.call('owned',cid=str(cid),sender=sender)
 context=await app.state.store.call('context',cid=str(cid),tid=str(tid))
 return {'products':context.get('selectedProducts',[]),'snapshotId':context.get('snapshotId')}
async def saved_report(cid,tid):
 from app.turn_report import report
 _,turn=await app.state.store.call('replay',cid=cid,tid=tid,after=0)
 context=await app.state.store.call('context',cid=cid,tid=tid)
 return report(cid,turn,context)

def report_response(data,format):
 from app.turn_report import text_report
 if format=='text':return Response(text_report(data),media_type='text/plain; charset=utf-8',headers={'Content-Disposition':f"attachment; filename=chat-turn-{data['turnId']}.txt"})
 return JSONResponse(data)

@app.get(TEST_BASE+'/{conversationId}/turns/{turnId}/report')
@app.get(BASE+'/{conversationId}/turns/{turnId}/report')
async def service_report(conversationId:UUID4,turnId:UUID4,request:Request,auth:Service,format:Literal['json','text']='json'):
 cid,tid=str(conversationId),str(turnId);await selected_runtime(cid,request)
 return report_response(await saved_report(cid,tid),format)

@app.get('/demo/conversations/{cid}/turns/{tid}/report',include_in_schema=False)
async def demo_report(cid:UUID4,tid:UUID4,sender:Demo,format:Literal['json','text']='json'):
 await app.state.store.call('owned',cid=str(cid),sender=sender)
 return report_response(await saved_report(str(cid),str(tid)),format)

@app.get('/demo/images/{filename}',include_in_schema=False)
async def demo_image(filename:str,sender:Demo):
 if getattr(app.state,'runtime_mode','live')=='mock':raise APIError(404,'고정 응답 모드에는 외부 상품 이미지가 없습니다.')
 import re
 if not re.fullmatch(r'[a-f0-9]+-\d+\.(avif|webp)',filename):raise APIError(404,'이미지를 찾을 수 없습니다.')
 r=await app.state.search._http.get('/images/'+filename)
 if r.status_code!=200:raise APIError(404,'이미지를 찾을 수 없습니다.')
 return Response(r.content,media_type=r.headers.get('content-type','image/webp'))
class Feedback(StrictModel):
 category:Literal['other','quality','constraints','latency','error']='other'
 submission_id:UUID4
 verdict:Literal['helpful','unhelpful','issue']
 note:str=Field(default='',max_length=2000)
 reporter_name:str=Field(default='',max_length=80)
@app.post('/demo/conversations/{cid}/turns/{tid}/feedback',include_in_schema=False)
async def feedback(cid:UUID4,tid:UUID4,body:Feedback,sender:Demo):
 await app.state.store.call('owned',cid=str(cid),sender=sender)
 throttle(('feedback',sender),60,3600)
 return await app.state.store.call('feedback',cid=str(cid),tid=str(tid),fid=str(body.submission_id),payload=body.model_dump(mode='json',by_alias=True,exclude={'submission_id'}))
@app.post('/demo/conversations/{cid}/reports',include_in_schema=False)
async def conversation_report(cid:UUID4,body:Feedback,sender:Demo):
 if not 3<=len(body.note.strip())<=1500:raise APIError(400,'문제와 기대한 동작을 3~1500자로 적어 주세요.')
 await app.state.store.call('owned',cid=str(cid),sender=sender)
 throttle(('feedback',sender),60,3600)
 return await app.state.store.call('conversation_feedback',cid=str(cid),fid=str(body.submission_id),payload=body.model_dump(mode='json',by_alias=True,exclude={'submission_id'}))
@app.get('/demo/reports',include_in_schema=False)
async def my_reports(sender:Demo,limit:Annotated[int,Query(ge=1,le=100)]=50,cursor:Annotated[str|None,Query(max_length=400)]=None):return await app.state.store.call('own_feedback',sender=sender,limit=limit,cursor=cursor)
@app.get('/demo/reports/{fid}',include_in_schema=False)
async def my_report(fid:UUID4,sender:Demo):return await app.state.store.call('own_feedback',sender=sender,fid=str(fid))
app.include_router(admin_router)

# Validation is intentionally HTTP 400 throughout this service contract.
def api_schema():
 from fastapi.openapi.utils import get_openapi
 if app.openapi_schema is None:
  schema=get_openapi(title=app.title,version=app.version,routes=app.routes)
  for path in schema['paths'].values():
   for operation in path.values():
    responses=operation.get('responses',{})
    responses.pop('422',None)
  app.openapi_schema=schema
 return app.openapi_schema
app.openapi=api_schema

# Minimal FE/BE example: auth may reuse the existing UI session; all chat I/O uses HTTP.
from app.minimal_chat.server import create_app as create_minimal_chat
async def minimal_identity(request):
 try:return await demo_session(request)
 except APIError as exc:
  if exc.status in (401,403):return None
  raise
app.mount('/minimal-chat',create_minimal_chat(upstream=lambda:app.state.proxy,existing_identity=minimal_identity))
