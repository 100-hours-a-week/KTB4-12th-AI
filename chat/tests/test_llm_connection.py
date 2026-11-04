"""Memory-only routing and real httpx redirect behavior with synthetic transports."""
import asyncio,json,socket
import httpx,pytest
from app.errors import APIError
from app.llm_connection import Connections,validate,destination,ACTIVE_CONNECTION,PinnedTransport
BASE='https://model.example/v1'
KEY='synthetic-distinct-endpoint-key'
def config():return {'kind':'openai-compatible','baseUrl':BASE,'model':'synthetic-vllm','connectTimeout':10,'readTimeout':75}
@pytest.fixture
def settings():return Connections()

@pytest.mark.parametrize('url',['https://fresh.example/v1','https://fresh.example/proxy/v1','https://fresh.example'])
def test_no_cloud_allowlist_and_normal_url_paths(settings,monkeypatch,url):
 monkeypatch.delenv('CHAT_LLM_ALLOWED_BASE_URLS',raising=False)
 assert validate({**config(),'baseUrl':url})['baseUrl'].endswith('/v1')

@pytest.mark.parametrize('url',['http://remote.example/v1','https://user:password@model.example/v1','https://169.254.169.254/v1','https://127.0.0.1/v1','https://model.example/v1?key=hidden','file:///v1','https://model.example\\evil/v1'])
def test_invalid_destinations_are_rejected(url):
 with pytest.raises(APIError):validate({**config(),'baseUrl':url})

@pytest.mark.parametrize('value',[None,[],{},True])
def test_malformed_config_has_safe_error(value):
 with pytest.raises(APIError):validate(value)

@pytest.mark.parametrize('status,method,other_host',[(307,'POST',False),(308,'POST',False),(303,'GET',False),(307,'POST',True)])
async def test_redirects_preserve_protocol_without_cross_origin_credentials(settings,status,method,other_host):
 seen=[]
 def reply(req):
  seen.append((req.method,str(req.url),req.headers.get('Authorization')))
  if len(seen)==1:return httpx.Response(status,headers={'Location':('https://result.example' if other_host else BASE)+'/result?request=synthetic'})
  return httpx.Response(200,text='data: [DONE]\n\n')
 c=validate(config())
 async with await settings.client(c,httpx.MockTransport(reply),token=KEY) as client:
  r=await client.post('chat/completions',json={'synthetic':True})
 assert r.status_code==200 and len(seen)==2
 assert seen[1][0]==method and seen[0][2]=='Bearer '+KEY
 assert seen[1][2]==(None if other_host else 'Bearer '+KEY)

@pytest.mark.parametrize('location',['http://remote.example/result','https://169.254.169.254/result'])
async def test_redirect_cannot_downgrade_or_reach_metadata(settings,location):
 calls=[]
 def reply(req):calls.append(req);return httpx.Response(307,headers={'Location':location})
 async with await settings.client(validate(config()),httpx.MockTransport(reply),token=KEY) as client:
  with pytest.raises(APIError):await client.post('chat/completions',json={})
 assert len(calls)==1

async def test_redirect_loop_is_bounded_without_auto_provider_retry(settings):
 calls=[]
 def reply(req):calls.append(req);return httpx.Response(307,headers={'Location':'/again'})
 async with await settings.client(validate(config()),httpx.MockTransport(reply),token=KEY) as client:
  with pytest.raises(httpx.TooManyRedirects):await client.post('chat/completions',json={})
 assert len(calls)==6

async def test_dns_private_result_never_reaches_transport(monkeypatch):
 monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(2,1,6,'',('10.0.0.1',443))])
 transport=PinnedTransport()
 try:
  with pytest.raises(APIError):await transport.handle_async_request(httpx.Request('POST',BASE+'/chat/completions'))
 finally:await transport.aclose()

async def test_key_free_start_and_missing_default_key(settings,monkeypatch):
 assert settings.public()['config']['kind']=='openrouter'
 assert (await settings.admit()).client is None
 monkeypatch.setattr(settings,'openrouter_ready',lambda:False)
 with pytest.raises(APIError):await settings.activate(config(),KEY,settings.revision)
 assert not settings.public()['endpointActive']
 with pytest.raises(APIError):await settings.admit()

@pytest.mark.parametrize('tool_response',[True,False])
async def test_compatible_runtime_resolves_text_or_tool_before_publishing(settings,tool_response):
 import time
 from app.llm_connection import AdmittedConnection
 from app.model_config import ModelConfig
 from app.low_latency import ProviderAdapter
 from types import SimpleNamespace
 packets=[{'choices':[{'delta':{'content':'조회하겠습니다.'}}]}]
 if tool_response:
  packets.append({'choices':[{'delta':{'tool_calls':[{'index':0,'id':'one','function':{'name':'gift_context','arguments':'{"queries":["책"]}'}}]},'finish_reason':'tool_calls'}]})
 else:packets.append({'choices':[{'delta':{'content':' 안녕하세요.'},'finish_reason':'stop'}]})
 packets.append({'choices':[],'usage':{'prompt_tokens':12,'completion_tokens':8}})
 wire=''.join('data: '+json.dumps(p,ensure_ascii=False)+'\n\n' for p in packets)+'data: [DONE]\n\n'
 async with httpx.AsyncClient(base_url=BASE,transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},text=wire))) as client:
  connection=AdmittedConnection('openai-compatible',BASE,'synthetic-vllm',1,client)
  config_model=connection.model_config()
  class Runtime:
   model_config=config_model
   def request_body(self,*args):return {'model':'synthetic-vllm','max_tokens':1200,'messages':[]}
  receipt={'startedMonotonic':time.monotonic()};token=ACTIVE_CONNECTION.set(connection)
  try:events=[v async for v in ProviderAdapter(Runtime(),receipt).stream([], [{'type':'function','function':{'name':'gift_context'}}], 'synthetic-vllm')]
  finally:ACTIVE_CONNECTION.reset(token)
  assert receipt['requestAudit'][0]['status']=='completed'
  assert receipt['requestAudit'][0]['usage']['completion_tokens']==8
  assert [e for e in events if e['kind']=='usage']
  text=''.join(e['value'] for e in events if e['kind']=='text')
  assert text==('' if tool_response else '조회하겠습니다. 안녕하세요.')
  assert bool([e for e in events if e['kind']=='tool'])==tool_response

async def test_redirect_userinfo_is_rejected(settings):
 async with await settings.client(validate(config()),httpx.MockTransport(lambda r:httpx.Response(307,headers={'Location':'https://user:secret@model.example/result'})),token=KEY) as client:
  with pytest.raises(APIError):await client.post('chat/completions',json={})

async def test_pinned_transport_preserves_host_tls_and_honors_dns_timeout(monkeypatch):
 monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(2,1,6,'',('8.8.8.8',443))])
 transport=PinnedTransport();await transport.inner.aclose();seen=[]
 class Inner:
  async def handle_async_request(self,r):seen.append(r);return httpx.Response(200)
  async def aclose(self):pass
 transport.inner=Inner()
 await transport.handle_async_request(httpx.Request('POST',BASE+'/chat/completions',headers={'Authorization':'Bearer '+KEY}))
 assert seen[0].url.host=='8.8.8.8' and seen[0].headers['host']=='model.example' and seen[0].extensions['sni_hostname']=='model.example'
 async def timeout(*a,**kw):
  a[0].close();raise TimeoutError()
 monkeypatch.setattr(asyncio,'wait_for',timeout)
 with pytest.raises(httpx.ConnectTimeout):await transport.handle_async_request(httpx.Request('POST',BASE+'/chat/completions'))
 await transport.aclose()
