"""Offline incident diagnostics: safe metadata, not private request bodies."""
import json
from uuid import uuid4
from pathlib import Path
import httpx
import pytest
from app.errors import APIError
from app.low_latency import LowLatencyRuntime
from app.request_audit import trace, conversation_projection
from app import release
from test_low_latency import Store

@pytest.mark.parametrize('exc,expected,source',[(APIError(503,'PRIVATE_CANARY'),503,'application'),(httpx.HTTPStatusError('PRIVATE_CANARY',request=httpx.Request('GET','https://example.invalid'),response=httpx.Response(502)),502,'upstream_response'),(RuntimeError('PRIVATE_CANARY'),None,None)])
async def test_failure_saved_exported_and_logged_without_private_message(exc,expected,source,caplog):
 store=Store();cid,tid=str(uuid4()),str(uuid4())
 await store.call('conversation',cid=cid,sender=7,recipient=42)
 await store.call('start',cid=cid,tid=tid,message='PRIVATE_INPUT_CANARY')
 class Runtime(LowLatencyRuntime):
  async def generate(self,cid,tid,receipt):
   receipt['activeStage']='recipient_profile'
   raise exc
 runtime=object.__new__(Runtime);runtime.init_state(store,None,'owner');runtime.model='synthetic'
 await runtime.run(cid,tid)
 ctx=store.traces[tid];state=store.public(store.states[tid])
 out=conversation_projection({'conversationId':cid,'turns':[{'state':state,'context':ctx}]})
 d=out['turns'][0]['trace']['diagnostic']
 assert d['httpStatus']==expected and d['httpStatusSource']==source
 assert d['stage']=='recipient_profile' and d['exceptionType']==type(exc).__name__
 assert ctx['elapsedMs']>=0
 assert f'turn={tid}' in caplog.text and 'stage=recipient_profile' in caplog.text and 'elapsedMs=' in caplog.text
 assert 'PRIVATE_CANARY' not in caplog.text+json.dumps(out)
 assert 'PRIVATE_INPUT_CANARY' not in json.dumps(out)

@pytest.mark.parametrize('status,code', [('UNAVAILABLE','recipient_profile_unavailable'),('AVAILABLE','recipient_profile_stale')])
async def test_profile_early_failure_has_safe_reason(status,code):
 store=Store();cid,tid=str(uuid4()),str(uuid4())
 await store.call('conversation',cid=cid,sender=7,recipient=42)
 await store.call('start',cid=cid,tid=tid,message='synthetic')
 class Runtime(LowLatencyRuntime):
  async def recipient_profile(self,cid,tid):return {'source':'AI_PROFILE_STORE','status':status,'data':{}}
 runtime=object.__new__(Runtime);runtime.init_state(store,None,'owner');runtime.model='synthetic'
 await runtime.run(cid,tid)
 saved=trace(cid,store.public(store.states[tid]),store.traces[tid])
 d=saved['diagnostic']
 assert d['stage']=='recipient_profile' and d['code']==code and d['category']==code
 assert d['httpStatus']==503 and d['httpStatusSource']=='application'
 assert saved['recording']['calls']=='not-started' and saved['totals']['attemptsObserved']==0
 assert [(s['name'],s['status']) for s in saved['timeline'] if s['kind']=='preparation']==[
  ('conversation_read','completed'),('recipient_profile','failed')]


async def test_early_store_error_retains_stage_timing_and_code_location_without_private_values():
 class BrokenStore(Store):
  async def call(self,method,**kwargs):
   if method=='read':raise RuntimeError('PRIVATE_ERROR_CANARY')
   return await super().call(method,**kwargs)
 store=BrokenStore();cid,tid=str(uuid4()),str(uuid4())
 await store.call('conversation',cid=cid,sender=7,recipient=42)
 await store.call('start',cid=cid,tid=tid,message='PRIVATE_INPUT_CANARY')
 runtime=object.__new__(LowLatencyRuntime);runtime.init_state(store,None,'owner');runtime.model='synthetic'
 await runtime.run(cid,tid)
 saved=trace(cid,store.public(store.states[tid]),store.traces[tid])
 assert saved['diagnostic']['stage']=='conversation_read'
 assert saved['recording']['calls']=='not-started'
 step=saved['timeline'][0]
 assert step['kind']=='preparation' and step['status']=='failed' and step['elapsedMs']>=0
 assert step['errorType']=='RuntimeError'
 frame=saved['diagnostic']['frames'][-1]
 assert frame['module']=='app.low_latency' and frame['function']=='_generate' and frame['line']>0
 assert 'PRIVATE_ERROR_CANARY' not in json.dumps(saved) and 'PRIVATE_INPUT_CANARY' not in json.dumps(saved)


def test_profile_state_and_versions_survive_projection_without_preferences():
 from app.request_audit import list_summary
 ctx={'agentFailureCode':'recipient_profile_stale','agentFailureStage':'recipient_profile',
      'failureStatus':503,'failureStatusSource':'application',
      'recipientProfile':{'status':'AVAILABLE','analysisStatus':'PENDING','analysisCurrent':False,
        'inputSourceVersion':12,'analysisSourceVersion':11,
        'data':{'sourceVersion':12,'giftPreference':'PRIVATE_PREFERENCE_CANARY'}}}
 detailed=trace('cid',{'turnId':'tid','status':'FAILED'},ctx)['diagnostic']
 summary=list_summary({'status':'FAILED',**ctx})['diagnostic']
 assert summary==detailed
 assert summary['profile']['inputSourceVersion']==12 and summary['profile']['analysisSourceVersion']==11
 assert summary['profile']['analysisStatus']=='PENDING'
 assert 'PRIVATE_PREFERENCE_CANARY' not in json.dumps(summary)
 assert summary['httpStatusSource']=='application'


def test_bad_profile_metadata_and_frames_are_not_promoted_to_diagnostic_evidence():
 ctx={'recipientProfile':{'status':'PRIVATE_CANARY','analysisCurrent':1,'inputSourceVersion':True},
      'failureFrames':[{'module':'untrusted.secret','function':'PRIVATE_CANARY','line':5},
                       {'module':'app.runtime','function':'run','line':True}]}
 d=trace('cid',{'turnId':'tid','status':'FAILED'},ctx)['diagnostic']
 assert d['profile']['status'] is None and d['profile']['inputSourceVersion'] is None
 assert d['profile']['analysisCurrent'] is None and d['frames']==[]
 assert 'PRIVATE_CANARY' not in json.dumps(d)


def test_build_identity_does_not_infer_from_fingerprint(tmp_path,monkeypatch):
 monkeypatch.setattr(release,'__file__',str(tmp_path/'release.py'))
 assert release.build_commit() is None
 p=tmp_path/'build-revision.txt';p.write_text('a'*40)
 assert release.build_commit()=='a'*40
 p.write_text('secret-token')
 assert release.build_commit() is None
 out=trace('cid',{'turnId':'tid','status':'FAILED'},{'promptVersion':'ac341aa9ee732941','buildCommit':'secret-token'})
 assert out['sourceIdentity']['buildCommit'] is None
 assert out['diagnostic']['stage'] is None


def test_production_build_persists_source_sha_in_chat_image():
 root=Path(__file__).parents[2]
 docker=(root/'integration/containers/Dockerfile').read_text()
 workflow=(root/'.github/workflows/publish-production-images.yml').read_text()
 assert 'ARG SOURCE_SHA' in docker
 assert '"$SOURCE_SHA" > /workspace/chat/app/build-revision.txt' in docker
 assert 'SOURCE_SHA=${{ github.sha }}' in workflow


@pytest.mark.parametrize('context,stage,code,status', [
 ({'failureStage':'context','failure':'APIError','elapsedMs':13},'context','APIError',None),
 ({'failureStage':'search','failure':'HTTPStatusError','failureStatus':503,'elapsedMs':13},'search','HTTPStatusError',503),
 ({'elapsedMs':13},None,None,None),
])
def test_old_saved_context_projects_only_recorded_evidence(context,stage,code,status):
 out=conversation_projection({'conversationId':'synthetic','turns':[{'state':{'turnId':'old','status':'FAILED'},'context':context}]})
 t=out['turns'][0]['trace'];d=t['diagnostic']
 assert (d['stage'],d['code'],d['httpStatus'])==(stage,code,status)
 assert d['code'] not in {'recipient_profile_unavailable','recipient_profile_stale'}
 assert t['elapsedMs']==13 and t['sourceIdentity']['buildCommit'] is None
