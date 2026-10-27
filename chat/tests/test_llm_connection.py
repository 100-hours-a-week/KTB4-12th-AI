import asyncio, copy, json, os
import httpx, pytest
from app.errors import APIError
from app.llm_connection import Connections, validate, destination, ACTIVE_CONNECTION
from app.low_latency import ProviderAdapter

BASE='http://127.0.0.1:18999/v1'
def config():return {'kind':'openai-compatible','baseUrl':BASE,'model':'synthetic-vllm','auth':'none','secretRef':None,'connectTimeout':2,'readTimeout':5}
def packet(name='connection_probe',arguments='{"probe":"ok"}'):
 return 'data: '+json.dumps({'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'name':name,'arguments':arguments}}]},'finish_reason':'tool_calls'}]})+'\n\ndata: [DONE]\n\n'
@pytest.fixture
def settings(tmp_path,monkeypatch):
 monkeypatch.setenv('AI_ENVIRONMENT','local');monkeypatch.setenv('CHAT_LLM_ALLOWED_BASE_URLS',BASE)
 return Connections(tmp_path/'model.json')
async def checked(settings,c=None):
 c=c or config()
 return await settings.check(c,httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},text=packet())))

@pytest.mark.parametrize('url',['http://169.254.169.254/v1','https://user:pass@example.com/v1','https://x.example/v1?key=a','file:///v1','http://localhost:18999/v1',BASE+'/chat/completions',BASE+'#secret'])
def test_url_rejects_unapproved_or_credential_destinations(settings,url):
 c=config();c['baseUrl']=url
 with pytest.raises(APIError):validate(c)
def test_remote_dns_private_ip_is_rejected(monkeypatch):
 monkeypatch.setenv('CHAT_LLM_ALLOWED_BASE_URLS','https://model.example/v1')
 monkeypatch.setattr('socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',('127.0.0.1',443))])
 with pytest.raises(APIError):destination('https://model.example/v1')
def test_production_never_accepts_local_mock(settings,monkeypatch):
 monkeypatch.setenv('AI_ENVIRONMENT','production')
 with pytest.raises(APIError):validate(config())
async def test_save_requires_checked_exact_settings_and_preserves_previous_on_error(settings):
 old=settings.public()
 with pytest.raises(APIError):await settings.publish(config(),0)
 assert settings.public()==old and not settings.path.exists()
 await checked(settings);saved=await settings.publish(config(),0);assert saved['revision']==1
 c=config();c['model']='different'
 with pytest.raises(APIError):await settings.publish(c,1)
 assert settings.read()['config']['model']=='synthetic-vllm'
 with pytest.raises(APIError):await settings.publish(config(),0)
 assert settings.read()['revision']==1
async def test_snapshot_and_rollback_publish_new_revision(settings):
 await checked(settings);await settings.publish(config(),0);admitted=await settings.admit()
 try:
  other=config();other['model']='new-model';await checked(settings,other);await settings.publish(other,1)
  assert admitted.model=='synthetic-vllm' and admitted.revision==1
  restored=await settings.rollback(2);assert restored['revision']==3 and restored['config']['model']=='synthetic-vllm'
 finally:await admitted.close()
@pytest.mark.parametrize('mode',['http','json','tool','incomplete'])
async def test_invalid_capabilities_never_replace_settings(settings,mode):
 def reply(req):
  if mode=='http':return httpx.Response(401,text='do not log this body')
  if mode=='json':return httpx.Response(200,json={'choices':[]})
  return httpx.Response(200,headers={'content-type':'text/event-stream'},text=packet('wrong') if mode=='tool' else packet().replace('data: [DONE]',''))
 with pytest.raises(APIError):await settings.check(config(),httpx.MockTransport(reply))
 assert settings.read()['revision']==0 and not settings.path.exists()
async def test_secret_reference_never_returns_or_reuses_openrouter_key(settings,monkeypatch):
 monkeypatch.setenv('CHAT_LLM_SECRET_REFS','SYNTHETIC_MODAL_KEY');monkeypatch.setenv('SYNTHETIC_MODAL_KEY','synthetic-modal-only');monkeypatch.setenv('OPENROUTER_API_KEY_CHAT','synthetic-openrouter-never-forward')
 c=config();c.update(auth='secret-ref',secretRef='SYNTHETIC_MODAL_KEY');seen=[]
 def reply(req):seen.append(req.headers.get('Authorization'));return httpx.Response(200,headers={'content-type':'text/event-stream'},text=packet())
 await settings.check(c,httpx.MockTransport(reply));await settings.publish(c,0)
 assert seen==['Bearer synthetic-modal-only']
 assert 'synthetic-modal-only' not in json.dumps(settings.public())
 assert 'synthetic-openrouter' not in json.dumps(settings.public())
async def test_redirect_not_followed(settings):
 seen=[]
 def reply(req):seen.append(str(req.url));return httpx.Response(307,headers={'Location':'http://169.254.169.254/v1/chat/completions'})
 with pytest.raises(APIError):await settings.check(config(),httpx.MockTransport(reply))
 assert seen==[BASE+'/chat/completions']
async def test_cancellation_closes_probe_and_does_not_save(settings):
 entered=asyncio.Event()
 async def reply(req):entered.set();await asyncio.Event().wait()
 task=asyncio.create_task(settings.check(config(),httpx.MockTransport(reply)));await entered.wait();task.cancel()
 with pytest.raises(asyncio.CancelledError):await task
 assert not settings.path.exists()
async def test_https_default_port_and_pinned_transport_host_sni(settings,monkeypatch):
 from app.llm_connection import PinnedTransport
 monkeypatch.setenv('CHAT_LLM_ALLOWED_BASE_URLS','https://model.example/v1')
 monkeypatch.setattr('socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',('8.8.8.8',443))])
 c=config();c['baseUrl']='https://model.example/v1';seen=[]
 def reply(req):seen.append(req.url);return httpx.Response(200,headers={'content-type':'text/event-stream'},text=packet())
 await settings.check(c,httpx.MockTransport(reply));assert seen[0].host=='model.example'
 class Inner(httpx.AsyncBaseTransport):
  async def handle_async_request(self,req):
   assert req.url.host=='8.8.8.8' and req.headers['host']=='model.example' and req.extensions['sni_hostname']=='model.example'
   return httpx.Response(200)
 transport=PinnedTransport('model.example','8.8.8.8');await transport.inner.aclose();transport.inner=Inner()
 async with httpx.AsyncClient(transport=transport) as client:assert (await client.get('https://model.example/v1/chat/completions')).status_code==200
async def test_real_chat_runtime_uses_compatible_snapshot_for_entire_turn(settings,monkeypatch):
 import uuid
 import test_low_latency as fixture
 from app.low_latency import LowLatencyRuntime
 from app.model_config import ModelCatalog
 from app.request_audit import trace
 await checked(settings);await settings.publish(config(),0)
 entered=asyncio.Event();release=asyncio.Event();sent=[]
 packets=[fixture.tool('gift_context',fixture.context(budget=20000)),fixture.tool('show_gifts',fixture.show())]
 async def reply(req):
  sent.append(json.loads(req.content))
  if len(sent)==1:entered.set();await release.wait()
  assert req.url==BASE+'/chat/completions' and 'authorization' not in req.headers
  assert 'provider' not in sent[-1] and 'reasoning' not in sent[-1]
  return httpx.Response(200,headers={'content-type':'text/event-stream'},text='data: '+json.dumps(packets.pop(0))+'\n\ndata: [DONE]\n\n')
 original=settings.client
 async def client(c,transport=None):return await original(c,transport or httpx.MockTransport(reply))
 monkeypatch.setattr(settings,'client',client)
 store=fixture.Store();search=fixture.Search();runtime=object.__new__(LowLatencyRuntime);runtime.init_state(store,search,'owner');runtime.connections=settings;runtime.model='unused/openrouter'
 async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(AssertionError('OpenRouter must not be called')))) as original_http:
  runtime.http=original_http;runtime.catalog=ModelCatalog(original_http)
  cid,tid=str(uuid.uuid4()),str(uuid.uuid4());await store.call('conversation',cid=cid,sender=7,recipient=42,protocol_fixture=False,demo_profile=None)
  accepted=await runtime.accept(cid,tid,'합성 책받침 하나 추천');assert accepted['status']=='PROCESSING'
  await entered.wait();old=settings.read();await settings.publish(copy.deepcopy(settings.default),old['revision']);release.set()
  await asyncio.gather(*list(runtime.jobs.values()))
  assert store.states[tid]['status']=='COMPLETED' and len(sent)==2 and all(b['model']=='synthetic-vllm' for b in sent)
  assert store.events[tid][-1]['event']=='turn.finished'
  receipt=store.traces[tid];assert receipt['connection']['revision']==1
  summary=trace(cid,store.public(store.states[tid]),receipt);assert summary['totals']['inputTokens']==20 and summary['totals']['outputTokens']==20
  # Same terminal turn retry is a read, never a new request/config admission.
  again=await runtime.accept(cid,tid,'합성 책받침 하나 추천');assert again['status']=='COMPLETED' and len(sent)==2
@pytest.mark.parametrize('mode',['text-before-tool','missing-finish','malformed-details'])
async def test_probe_matches_runtime_tool_stream_contract(settings,mode):
 def reply(req):
  raw=packet()
  if mode=='missing-finish':raw=raw.replace('"tool_calls"}', 'null}')
  elif mode=='text-before-tool':raw='data: '+json.dumps({'choices':[{'delta':{'content':'narration'}}]})+'\n\n'+raw
  else:raw='data: []\n\ndata: [DONE]\n\n'
  return httpx.Response(200,headers={'content-type':'text/event-stream'},text=raw)
 with pytest.raises(APIError):await settings.check(config(),httpx.MockTransport(reply))
 assert settings.read()['revision']==0
