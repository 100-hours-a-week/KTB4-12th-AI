"""Reusable connections: isolated files and scripted providers, no paid calls."""
import copy,json
import httpx,pytest
from app.errors import APIError
from app.llm_connection import Connections
from test_llm_connection import config,settings,checked

async def test_save_apply_switch_back_persist_and_repeat_without_provider_calls(settings,monkeypatch):
 saved=await settings.save_profile('modal','Modal Qwen',config(),0)
 assert saved['config']['kind']=='openrouter' and saved['activeProfileId']=='openrouter'
 assert saved['profiles'][-1]['verification']['state']=='needs-check'
 settings._credential=None
 with pytest.raises(APIError):await settings.apply_profile('modal',1)
 assert settings.read()['revision']==1
 from app.llm_connection import fingerprint
 settings._credential=(fingerprint(config()),'wk-synthetic.ws-synthetic')
 await checked(settings)
 saved=await settings.save_profile('modal','Modal Qwen',config(),1)
 assert saved['revision']==2
 async def no_client(*args,**kwargs):raise AssertionError('Selection/save/apply must not generate a provider call')
 monkeypatch.setattr(settings,'client',no_client)
 active=await settings.apply_profile('modal',2)
 assert active['activeProfileId']=='modal' and active['config']['model']=='synthetic-vllm'
 again=await settings.apply_profile('modal',3)
 assert again['revision']==3
 loaded=Connections(settings.path)
 assert loaded.read()['profiles']==active['profiles']
 monkeypatch.setattr(loaded,'client',no_client)
 assert (await loaded.apply_profile('openrouter',3))['config']['kind']=='openrouter'
 with pytest.raises(APIError):await loaded.apply_profile('modal',4)
 assert loaded.read()['profiles'][-1]['verification']['checkedAt']==active['profiles'][-1]['verification']['checkedAt']

async def test_edit_invalidates_only_changed_connection_and_keeps_active_config(settings):
 await checked(settings);await settings.save_profile('modal','Modal',config(),0);await settings.apply_profile('modal',1)
 renamed=await settings.save_profile('modal','Modal 이름 수정',config(),2)
 assert renamed['profiles'][-1]['verification']['state']=='verified'
 changed=config();changed['model']='changed-model'
 edited=await settings.save_profile('modal','Modal 이름 수정',changed,3)
 assert edited['config']['model']=='synthetic-vllm' and edited['activeProfileId'] is None
 assert edited['profiles'][-1]['verification']['state']=='needs-check'
 with pytest.raises(APIError):await settings.apply_profile('modal',4)
 assert settings.read()==edited_without_public_fields(edited)

def edited_without_public_fields(public):
 return {k:v for k,v in public.items() if k not in {'defaultConfig','writable','allowedBaseUrls','secretReferences','scope','verificationNotice','modal'}}

async def test_profiles_preserve_old_file_and_legacy_publish_rollback(settings):
 old=settings.read();old.pop('profiles');old.pop('activeProfileId');old['revision']=1
 settings.path.write_text(json.dumps(old));raw=settings.path.read_text()
 assert settings.read()['activeProfileId']=='openrouter' and settings.path.read_text()==raw
 await settings.save_profile('second','Luna second',settings.default,1)
 await settings.publish(copy.deepcopy(settings.default),2)
 assert len(settings.read()['profiles'])==2
 restored=await settings.rollback(3)
 assert restored['revision']==4 and len(restored['profiles'])==2

async def test_stale_repeated_conflicting_unknown_and_readonly_operations(settings):
 await settings.save_profile('one','One',settings.default,0);raw=settings.path.read_text()
 for action in [settings.save_profile('one','One',settings.default,0),settings.save_profile('two','One',settings.default,1),settings.apply_profile('missing',1)]:
  with pytest.raises(APIError):await action
  assert settings.path.read_text()==raw
 readonly=Connections()
 with pytest.raises(APIError):await readonly.save_profile('one','One',settings.default,0)
 with pytest.raises(APIError):await readonly.apply_profile('openrouter',0)

async def test_profile_credentials_remain_refs_and_inactive_policy_changes_do_not_break_listing(settings,monkeypatch):
 monkeypatch.setenv('CHAT_LLM_SECRET_REFS','NEUTRAL_CREDENTIAL');monkeypatch.setenv('NEUTRAL_CREDENTIAL','synthetic-profile-secret')
 c=config();c.update(auth='secret-ref',secretRef='NEUTRAL_CREDENTIAL')
 await checked(settings,c);await settings.save_profile('modal','Modal',c,0)
 assert 'synthetic-profile-secret' not in settings.path.read_text()+json.dumps(settings.public())
 monkeypatch.delenv('NEUTRAL_CREDENTIAL');monkeypatch.setenv('CHAT_LLM_ALLOWED_BASE_URLS','')
 assert settings.public()['config']['kind']=='openrouter'
 raw=settings.path.read_text()
 with pytest.raises(APIError):await settings.apply_profile('modal',1)
 assert settings.path.read_text()==raw

async def test_profile_limits_and_write_failure_preserve_file(settings,monkeypatch):
 for i in range(9):await settings.save_profile('p'+str(i),'Profile '+str(i),settings.default,i)
 raw=settings.path.read_text()
 with pytest.raises(APIError):await settings.save_profile('overflow','Overflow',settings.default,9)
 assert settings.path.read_text()==raw
 monkeypatch.setattr('os.replace',lambda *a:(_ for _ in ()).throw(OSError('synthetic write failure')))
 with pytest.raises(APIError):await settings.save_profile('p0','Renamed',settings.default,9)
 assert settings.path.read_text()==raw and not list(settings.path.parent.glob('.chat-connection-*'))

async def test_utf8_storage_limit_is_checked_before_replacing_file(settings):
 value=settings.read();value['revision']=1;value['history']=[{'config':{'padding':''}}]
 overhead=len(json.dumps(value,ensure_ascii=False).encode());value['history'][0]['config']['padding']='x'*(31800-overhead)
 raw=json.dumps(value,ensure_ascii=False);settings.path.write_text(raw)
 assert len(raw.encode())==31800
 with pytest.raises(APIError):await settings.save_profile('extra','추가 프로필',settings.default,1)
 assert settings.path.read_text()==raw

async def test_profile_routes_require_existing_admin_and_reject_cross_origin(settings,monkeypatch):
 import os
 from app import main,security
 from types import SimpleNamespace
 monkeypatch.setattr(main.app.state,'runtime',SimpleNamespace(connections=settings),raising=False);security.rate.clear()
 async with httpx.AsyncClient(base_url='https://synthetic',transport=httpx.ASGITransport(app=main.app)) as client:
  path='/admin/api/model-connection/profiles/named'
  body={'revision':0,'name':'Named','config':settings.default}
  assert (await client.put(path,json=body)).status_code==401
  assert (await client.post('/admin/login',json={'password':os.environ['QA_ADMIN_PASSWORD']})).status_code==200
  assert (await client.put(path,json=body,headers={'Origin':'https://evil.invalid'})).status_code==403
  result=await client.put(path,json=body);assert result.status_code==200
  assert result.json()['activeProfileId']=='openrouter'
  result=await client.post(path+'/apply',json={'revision':1});assert result.status_code==200
  assert result.json()['activeProfileId']=='named'

async def test_unavailable_active_profile_can_be_inspected_and_switched_back_safely(settings,monkeypatch):
 monkeypatch.setenv('CHAT_LLM_SECRET_REFS','NEUTRAL_CREDENTIAL');monkeypatch.setenv('NEUTRAL_CREDENTIAL','synthetic-profile-secret')
 c=config();c.update(auth='secret-ref',secretRef='NEUTRAL_CREDENTIAL')
 from app.llm_connection import fingerprint
 c.update(auth='session-key',secretRef=None);settings._credential=(fingerprint(c),'wk-synthetic.ws-synthetic')
 await checked(settings,c);await settings.save_profile('modal','Modal',c,0);await settings.apply_profile('modal',1)
 monkeypatch.delenv('NEUTRAL_CREDENTIAL');monkeypatch.setenv('CHAT_LLM_ALLOWED_BASE_URLS','')
 assert settings.public()['activeProfileId']=='modal'
 with pytest.raises(APIError):await settings.admit()
 restored=await settings.apply_profile('openrouter',2)
 assert restored['config']['kind']=='openrouter'
