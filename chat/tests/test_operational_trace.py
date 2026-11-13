"""Synthetic transport failures and safe operational projection, not model scores."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from app.low_latency import LowLatencyRuntime
from app.request_audit import trace, list_summary
from test_low_latency import Store
from test_report_timeout_followup import delayed_turn


@pytest.mark.parametrize('exception,category',[(httpx.ReadTimeout,'provider_read_timeout'),(httpx.ConnectTimeout,'provider_connect_timeout'),(httpx.WriteTimeout,'provider_write_timeout'),(httpx.PoolTimeout,'provider_pool_timeout')])
async def test_transport_timeout_cause_survives_agent_error_wrapping(exception,category):
 store,receipt,_,cid,tid=await delayed_turn([.001],error=exception('PRIVATE_ERROR_BODY_CANARY'))
 out=trace(cid,store.public(store.states[tid]),receipt)
 assert out['diagnostic']['category']==category
 assert out['diagnostic']['stage']=='model_1'
 call=out['modelCalls'][0]
 assert call['errorType']==exception.__name__ and call['httpStatus']==200
 assert call['headersMs']>=0 and call['firstPacketMs'] is None
 assert call['timeouts']['readSeconds'] is not None
 assert 'PRIVATE_ERROR_BODY_CANARY' not in json.dumps(out)


async def test_overall_deadline_is_distinct_from_provider_read_timeout():
 store,receipt,_,cid,tid=await delayed_turn([.08],timeout=.025)
 out=trace(cid,store.public(store.states[tid]),receipt)
 assert out['diagnostic']['category']=='turn_deadline'
 assert out['diagnostic']['turnTimeoutSeconds']==.025
 assert out['modelCalls'][0]['errorType']=='CancelledError'


async def test_success_records_real_milestones_and_unknown_delivery():
 store,receipt,_,cid,tid=await delayed_turn([.001])
 out=trace(cid,store.public(store.states[tid]),receipt)
 assert out['diagnostic']['category']=='completed'
 c=out['modelCalls'][0]
 assert 0<=c['headersMs']<=c['firstPacketMs']<=c['firstTextMs']<=c['elapsedMs']
 assert c['firstToolMs'] is None and c['errorType'] is None
 assert out['diagnostic']['delivery'].startswith('not-observed')
 assert out['diagnostic']['capturedAt']


async def test_progress_exists_before_completion_and_failures_do_not_fail_turn():
 store=Store();cid,tid=str(uuid4()),str(uuid4());release=asyncio.Event();written=asyncio.Event()
 await store.call('conversation',cid=cid,sender=7,recipient=42)
 await store.call('start',cid=cid,tid=tid,message='합성 대기')
 original=store.call
 async def call(method,**kwargs):
  result=await original(method,**kwargs)
  if method=='context' and kwargs.get('checkpoint'):written.set()
  return result
 store.call=call
 class Runtime(LowLatencyRuntime):
  async def generate(self,cid,tid,receipt):
   receipt['activeStage']='responder'
   receipt['requestAudit']=[{'index':1,'status':'running','offsetMs':0}]
   await release.wait()
 runtime=object.__new__(Runtime);runtime.init_state(store,None,'owner');runtime.model='synthetic-model'
 task=asyncio.create_task(runtime.run(cid,tid))
 await asyncio.wait_for(written.wait(),1)
 assert store.traces[tid]['tracePersistence']['status']=='pending'
 assert store.traces[tid]['activeStage']=='responder'
 assert store.traces[tid]['requestAudit'][0]['elapsedMs']>=0
 release.set();await task
 assert store.traces[tid]['outcome']=='COMPLETED'
 assert store.traces[tid]['tracePersistence']['status']=='recorded'
 assert store.states[tid]['status']=='COMPLETED'


async def test_checkpoint_storage_error_cannot_abort_generation():
 store=Store();cid,tid=str(uuid4()),str(uuid4());attempted=asyncio.Event()
 await store.call('conversation',cid=cid,sender=7,recipient=42)
 await store.call('start',cid=cid,tid=tid,message='합성 저장 장애')
 original=store.call
 async def call(method,**kwargs):
  if kwargs.get('checkpoint'):attempted.set();raise RuntimeError('synthetic unavailable')
  return await original(method,**kwargs)
 store.call=call
 class Runtime(LowLatencyRuntime):
  async def generate(self,cid,tid,receipt):await attempted.wait();await asyncio.sleep(0)
 runtime=object.__new__(Runtime);runtime.init_state(store,None,'owner');runtime.model='synthetic-model'
 await asyncio.wait_for(runtime.run(cid,tid),1)
 assert store.traces[tid]['checkpointWriteFailures']==1
 assert store.states[tid]['status']=='COMPLETED'


def test_legacy_failure_has_unknown_cause_and_summary_does_not_export_body():
 state={'turnId':'synthetic','status':'FAILED','lastEventId':1}
 out=trace('synthetic',state,{})
 assert out['diagnostic']['category']=='failed'
 assert out['diagnostic']['code'] is None and out['diagnostic']['turnTimeoutSeconds'] is None
 raw={**state,'agentFailureCode':'ReadTimeout','agentFailureStage':'model_2','lastModelCall':{'errorType':'ReadTimeout','httpStatus':200},'input':'PRIVATE_BODY_CANARY'}
 summary=list_summary(raw)
 assert summary['diagnostic']['category']=='provider_read_timeout'
 assert summary['diagnostic']['stage']=='model_2'
 assert 'PRIVATE_BODY_CANARY' not in json.dumps(summary)


def test_search_timeout_is_not_attributed_to_model_server():
 out=trace('cid',{'turnId':'tid','status':'FAILED'}, {'agentFailureCode':'ReadTimeout','agentFailureStage':'context','requestAudit':[{'status':'completed','httpStatus':200}]})
 assert out['diagnostic']['category']=='dependency_timeout'
 assert out['diagnostic']['stage']=='context'
 assert out['diagnostic']['httpStatus'] is None and out['diagnostic']['lastModelHttpStatus']==200


def test_search_http_error_is_not_replaced_by_previous_model_success():
 out=trace('cid',{'turnId':'tid','status':'FAILED'}, {'failureStatus':503,'agentFailureStage':'context','requestAudit':[{'status':'completed','httpStatus':200}]})
 assert out['diagnostic']['category']=='dependency_http_error' and out['diagnostic']['httpStatus']==503


async def test_running_preparation_checkpoint_records_elapsed_without_inputs_or_results():
 import time
 from app.diagnostics import preparation_step,snapshot_receipt
 receipt={'startedMonotonic':time.monotonic()}
 async with preparation_step(receipt,'recipient_profile'):
  frozen=snapshot_receipt(receipt)
  step=frozen['preparationSteps'][0]
  assert step['status']=='running' and step['elapsedMs']>=0 and step['elapsedFinal'] is False
  assert '_startedMonotonic' not in step and 'result' not in step and 'arguments' not in step
 assert receipt['preparationSteps'][0]['status']=='completed'


async def test_optional_metadata_failure_is_visible_without_blocking_ordinary_chat(monkeypatch):
 from test_low_latency import run,text,Search
 async def unavailable(self):raise httpx.ConnectError('PRIVATE_METADATA_CANARY')
 monkeypatch.setattr(Search,'metadata',unavailable)
 receipt,sent,store,_,cid,tid=await run([text('안녕하세요.')])
 out=trace(cid,store.public(store.states[tid]),receipt)
 step=next(s for s in out['timeline'] if s.get('name')=='catalog_metadata')
 assert step['status']=='unavailable' and step['errorType']=='ConnectError'
 assert len(sent)==1 and 'PRIVATE_METADATA_CANARY' not in json.dumps(out)
