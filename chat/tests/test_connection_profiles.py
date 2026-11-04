"""Configuration is ephemeral even when an old JSON path is still configured."""
import asyncio,json
import pytest
from app.errors import APIError
from app.llm_connection import Connections
from test_llm_connection import config,settings,KEY

async def test_no_files_read_written_or_profiles_restored(settings,tmp_path,monkeypatch):
 path=tmp_path/'old.json';raw='old user settings preserved';path.write_text(raw)
 monkeypatch.setenv('CHAT_LLM_CONFIG_FILE',str(path));monkeypatch.setenv('CHAT_LLM_ALLOWED_BASE_URLS','')
 initial=settings.revision
 async def forbidden(*a,**kw):raise AssertionError('Activation must not contact a model')
 monkeypatch.setattr(settings,'client',forbidden)
 active=await settings.activate(config(),KEY,initial)
 assert active['endpointActive'] and KEY not in json.dumps(active)+repr(settings)
 restarted=Connections()
 assert not restarted.public()['endpointActive'] and restarted.public()['config']['kind']=='openrouter'
 with pytest.raises(APIError):await restarted.activate(config(),KEY,active['revision'])
 assert path.read_text()==raw and list(tmp_path.iterdir())==[path]
 off=await settings.disable(active['revision'])
 assert not off['endpointActive'] and settings._key is None
 assert (await settings.disable(off['revision']))['revision']>off['revision']

async def test_parallel_changes_and_duplicate_actions(settings):
 rev=settings.revision
 results=await asyncio.gather(settings.activate(config(),KEY,rev),settings.activate({**config(),'model':'other'},KEY,rev),return_exceptions=True)
 assert sum(isinstance(x,APIError) for x in results)==1
 rev=settings.revision
 assert (await settings.activate(settings.read()['config'],KEY,rev))['revision']==rev
 await settings.disable(rev)
 with pytest.raises(APIError):await settings.activate(config(),KEY,rev)

async def test_failed_edit_keeps_prior_provider(settings):
 await settings.activate(config(),KEY,settings.revision);previous=settings.public()
 with pytest.raises(APIError):await settings.activate({**config(),'baseUrl':'http://bad.example'},'new',settings.revision)
 assert settings.public()==previous and settings._key==KEY

async def test_timeout_or_model_edit_can_use_same_endpoint_key(settings):
 await settings.activate(config(),KEY,settings.revision)
 edited={**config(),'model':'other','readTimeout':90}
 assert (await settings.activate(edited,KEY,settings.revision))['config']['readTimeout']==90

async def test_off_on_default_invalidates_delayed_activation(settings):
 old=settings.revision
 await settings.disable(old)
 with pytest.raises(APIError):await settings.activate(config(),KEY,old)
 assert not settings.public()['endpointActive']
