"""Transient endpoint credentials: synthetic only, no network/provider/DB."""
import asyncio,copy,json,os
import httpx,pytest
from app.errors import APIError
from app.llm_connection import Connections,fingerprint
from test_llm_connection import config,settings

KEY='wk-synthetic.ws-memory-only'

async def prepare(settings):
 settings._credential=None
 saved=await settings.save_profile('modal','Modal',config(),0)
 return await settings.submit_key(config(),KEY,saved['revision'])

async def test_key_entry_activation_off_restart_and_no_persisted_secret(settings,monkeypatch):
 async def forbidden(*a,**k):raise AssertionError('No call on entry/save/apply/off')
 monkeypatch.setattr(settings,'client',forbidden)
 state=await prepare(settings)
 assert state['config']['kind']=='openrouter' and state['modal']['keyPresent'] and not state['modal']['active']
 assert KEY not in json.dumps(state)+settings.path.read_text()+repr(settings)
 active=await settings.apply_profile('modal',state['revision'])
 assert active['modal']['active'] and active['config']==config()
 restarted=Connections(settings.path)
 assert restarted.public()['config']['kind']=='openrouter' and not restarted.public()['modal']['keyPresent']
 with pytest.raises(APIError):await restarted.apply_profile('modal',active['revision'])
 off=await settings.disable(active['revision'])
 assert off['config']['kind']=='openrouter' and not off['modal']['keyPresent']
 with pytest.raises(APIError):await settings.apply_profile('modal',off['revision'])
 assert KEY not in settings.path.read_text()

@pytest.mark.parametrize('token',['AKIAFAKE123456789012','ak-account.as-account','Bearer wk-fake.ws-fake','wk-fake.ws-fake\r\nX: bad',None,[],{'key':KEY},'x'*1000])
async def test_wrong_credential_types_are_safe(settings,token):
 settings._credential=None
 with pytest.raises(APIError) as e:await settings.submit_key(config(),token,0)
 assert KEY not in str(e.value) and not settings.path.exists()

async def test_edit_and_stale_mutations_cannot_reuse_or_restore_key(settings):
 await prepare(settings)
 changed=config();changed['model']='other'
 state=await settings.save_profile('modal','Modal',changed,2)
 assert state['modal']['readyProfiles']==[]
 with pytest.raises(APIError):await settings.apply_profile('modal',3)
 with pytest.raises(APIError):await settings.submit_key(config(),KEY,0)
 await settings.disable(3)
 with pytest.raises(APIError):await settings.submit_key(config(),KEY,3)
 assert not settings.public()['modal']['keyPresent']

async def test_parallel_activation_and_disable_revisions_are_serialized(settings):
 await prepare(settings)
 results=await asyncio.gather(settings.apply_profile('modal',2),settings.disable(2),return_exceptions=True)
 assert sum(isinstance(r,APIError) for r in results)==1
 latest=settings.public();off=await settings.disable(latest['revision'])
 assert not off['modal']['active'] and not off['modal']['keyPresent']

async def test_admitted_key_snapshot_survives_off_and_close_drops_header(settings,monkeypatch):
 await prepare(settings);await settings.apply_profile('modal',2)
 entered=asyncio.Event();release=asyncio.Event();seen=[]
 original=settings.client
 async def factory(c,transport=None,**kw):
  entered.set();await release.wait()
  return await original(c,httpx.MockTransport(lambda req:(seen.append(req.headers['Authorization']) or httpx.Response(200))),**kw)
 monkeypatch.setattr(settings,'client',factory)
 task=asyncio.create_task(settings.admit());await entered.wait()
 await settings.disable(3);release.set();admitted=await task
 await admitted.client.post('chat/completions',json={})
 assert seen==['Bearer '+KEY] and (await settings.admit()).kind=='openrouter'
 await admitted.close();assert 'authorization' not in admitted.client.headers

async def test_missing_openrouter_blocks_activation_but_off_clears_key(settings,monkeypatch):
 await prepare(settings);await settings.apply_profile('modal',2)
 monkeypatch.delenv('OPENROUTER_API_KEY',raising=False);monkeypatch.delenv('OPENROUTER_API_KEY_CHAT',raising=False)
 off=await settings.disable(3)
 assert not off['modal']['active'] and not off['modal']['keyPresent'] and not off['modal']['openrouterReady']
 with pytest.raises(APIError):await settings.admit()
 with pytest.raises(APIError):await settings.submit_key(config(),KEY,4)

async def test_secret_api_auth_origin_size_masking_and_no_echo(settings,monkeypatch,caplog):
 from app import main,security
 from types import SimpleNamespace
 settings._credential=None
 monkeypatch.setattr(main.app.state,'runtime',SimpleNamespace(connections=settings),raising=False);security.rate.clear()
 async with httpx.AsyncClient(base_url='https://synthetic',transport=httpx.ASGITransport(app=main.app)) as client:
  path='/admin/api/model-connection/modal-key';body={'config':config(),'key':KEY,'revision':0}
  assert (await client.post(path,json=body)).status_code==401
  await client.post('/admin/login',json={'password':os.environ['QA_ADMIN_PASSWORD']})
  assert (await client.post(path,json=body,headers={'Origin':'https://evil.invalid'})).status_code==403
  ok=await client.post(path,json=body);assert ok.status_code==200 and KEY not in ok.text
  bad=await client.post(path,json={'key':KEY});assert bad.status_code==400 and KEY not in bad.text
  large=await client.post(path,content=KEY*500);assert large.status_code==413 and KEY not in large.text
  assert KEY not in caplog.text+settings.path.read_text()
  off=await client.post('/admin/api/model-connection/modal-off',json={'revision':1});assert off.status_code==200

async def test_echoed_key_is_redacted_in_diagnostics_trace_and_closed_object_repr(settings,monkeypatch):
 from app.diagnostics import clean,snapshot_receipt
 from app.request_audit import capture_request
 from app.llm_connection import ACTIVE_CONNECTION
 await prepare(settings);await settings.apply_profile('modal',2)
 original=settings.client
 async def factory(c,transport=None,**kw):
  return await original(c,httpx.MockTransport(lambda req:httpx.Response(401,text=KEY)),**kw)
 monkeypatch.setattr(settings,'client',factory)
 admitted=await settings.admit();context=ACTIVE_CONNECTION.set(admitted)
 try:
  assert KEY not in repr(admitted)
  assert KEY not in json.dumps(clean({'providerEcho':KEY,'Authorization':'Bearer '+KEY}))
  assert KEY not in json.dumps(snapshot_receipt({'providerEcho':KEY}))
  assert KEY not in json.dumps(capture_request({'messages':[{'role':'user','content':KEY}]}))
 finally:ACTIVE_CONNECTION.reset(context);await settings.disable(3);await admitted.close()
 assert KEY not in repr(admitted)+str(admitted.client.headers)+json.dumps(settings.public())+settings.path.read_text()

async def test_off_clears_key_even_when_persistence_fails(settings,monkeypatch):
 await prepare(settings);await settings.apply_profile('modal',2)
 monkeypatch.setattr('os.replace',lambda *a:(_ for _ in ()).throw(OSError('synthetic failure')))
 with pytest.raises(APIError):await settings.disable(3)
 assert not settings.public()['modal']['active'] and not settings.public()['modal']['keyPresent']
 assert (await settings.admit()).kind=='openrouter'

async def test_modal_history_churn_preserves_openrouter_baseline_on_restart_and_off(settings):
 settings._credential=None
 baseline=copy.deepcopy(settings.default);baseline['readTimeout']=33
 await settings.publish(baseline,0)
 for i in range(6):await settings.save_profile('modal'+str(i),'Modal '+str(i),config(),i+1)
 await settings.submit_key(config(),KEY,7)
 for i in range(6):await settings.apply_profile('modal'+str(i),8+i)
 assert all(h['config']['kind']=='openai-compatible' for h in settings.read()['history'])
 restarted=Connections(settings.path)
 assert restarted.public()['config']==baseline
 assert (await settings.disable(14))['config']==baseline
 assert KEY not in settings.path.read_text()
