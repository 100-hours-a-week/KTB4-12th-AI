"""API authentication, key non-disclosure and admitted request lifetime."""
import asyncio,json,os
from types import SimpleNamespace
import httpx,pytest
from app.errors import APIError
from test_llm_connection import config,settings,KEY

@pytest.mark.parametrize('key',[None,[],{},'bad\r\nHeader: value','x'*4097])
async def test_invalid_key_never_enters_errors_or_state(settings,key,caplog):
 with pytest.raises(APIError) as error:await settings.activate(config(),key,settings.revision)
 assert KEY not in str(error.value)+caplog.text+json.dumps(settings.public())

@pytest.mark.parametrize('key',['','ordinary-vllm-key','wk-synthetic.ws-synthetic'])
async def test_vendor_neutral_inference_key(settings,key):
 assert (await settings.activate(config(),key,settings.revision))['endpointActive']
 assert key=='' or key not in json.dumps(settings.public())

async def test_admitted_request_keeps_original_provider_on_switch_off(settings,monkeypatch):
 await settings.activate(config(),KEY,settings.revision)
 started=asyncio.Event();release=asyncio.Event();seen=[];original=settings.client
 async def make(c,**kw):
  started.set();await release.wait()
  return await original(c,httpx.MockTransport(lambda req:(seen.append(req.headers.get('Authorization')) or httpx.Response(200))),**kw)
 monkeypatch.setattr(settings,'client',make)
 task=asyncio.create_task(settings.admit());await started.wait()
 await settings.disable(settings.revision);release.set();admitted=await task
 await admitted.client.post('chat/completions',json={})
 assert seen==['Bearer '+KEY] and (await settings.admit()).kind=='openrouter'
 await admitted.close();assert 'Authorization' not in admitted.client.headers

async def test_admin_origin_secret_errors_and_no_old_routes(settings,monkeypatch,caplog):
 from app import main,security
 monkeypatch.setattr(main.app.state,'runtime',SimpleNamespace(connections=settings),raising=False);security.rate.clear()
 body={'config':config(),'key':KEY,'revision':settings.revision};path='/admin/api/model-connection/activate'
 async with httpx.AsyncClient(base_url='https://synthetic',transport=httpx.ASGITransport(app=main.app)) as client:
  assert (await client.post(path,json=body)).status_code==401
  assert (await client.post('/admin/login',json={'password':os.environ['QA_ADMIN_PASSWORD']})).status_code==200
  assert (await client.post(path,json=body,headers={'Origin':'https://other.invalid'})).status_code==403
  result=await client.post(path,json=body);assert result.status_code==200
  assert KEY not in result.text+caplog.text
  bad=await client.post(path,json={'key':KEY});assert bad.status_code==400 and KEY not in bad.text
  oversized=await client.post(path,content=KEY*600);assert oversized.status_code==413 and KEY not in oversized.text
  stale=await client.post(path,json=body);assert stale.status_code==409 and KEY not in stale.text
  for old in ('check','rollback','modal-key'):assert (await client.post('/admin/api/model-connection/'+old,json={})).status_code==404
  off=await client.post('/admin/api/model-connection/modal-off',json={'revision':settings.revision});assert off.status_code==200
  await client.post('/admin/logout');assert (await client.get('/admin/api/model-connection')).status_code==401
