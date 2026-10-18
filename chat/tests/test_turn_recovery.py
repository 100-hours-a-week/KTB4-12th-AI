"""Real Runtime and HTTP handlers under local storage faults; no provider calls."""
import asyncio
import copy
import os
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault('AI_SERVICE_TOKEN','test-service-token')
os.environ.setdefault('SESSION_SECRET','local-test-session-secret')
os.environ.setdefault('QA_ADMIN_PASSWORD','local-test-admin')
os.environ.setdefault('DEMO_ACCESS_TOKEN','test-team-token')
from app import main
from app.errors import APIError
from app.runtime import Runtime
from app.security import SIGNER


class FaultStore:
 def __init__(self):
  self.turns={};self.starts=0;self.generation_calls=0;self.finished=[]
  self.start_entered=asyncio.Event();self.start_release=asyncio.Event();self.start_release.set()
  self.run_entered=asyncio.Event();self.run_release=asyncio.Event()
  self.lost_start_acks=0;self.start_failures=0;self.context_failures=0
  self.finish_failures=0;self.lost_finish_acks=0;self.context_calls=0
 async def call(self,method,**a):
  if method=='binding':return {'protocolFixture':False}
  if method=='existing_turn':
   old=self.turns.get(a['tid'])
   if old is None:return None
   if old['cid']!=a['cid'] or old['message']!=a['message'] or old.get('requested_model')!=a.get('model') or old.get('recipient_context')!=a.get('recipient_context'):raise APIError(409,'conflict')
   return self.snapshot(a['tid'])
  if method=='start':
   self.starts+=1
   if self.start_failures:
    self.start_failures-=1;raise APIError(503,'injected start failure')
   tid=a['tid'];made=tid not in self.turns
   if made:self.turns[tid]={'turnId':tid,'status':'PROCESSING','message':a['message'],'blocks':[],'lastEventId':0,'cid':a['cid'],'requested_model':a.get('model'),'recipient_context':a.get('recipient_context')}
   s=self.turns[tid]
   if s['cid']!=a['cid'] or s['message']!=a['message']:raise APIError(409,'conflict')
   self.start_entered.set();await self.start_release.wait()
   if self.lost_start_acks:self.lost_start_acks-=1;raise APIError(503,'injected lost commit acknowledgment')
   return [self.snapshot(tid),made]
  if method=='replay':return [[],self.snapshot(a['tid'])]
  if method=='context':
   self.context_calls+=1
   if self.context_failures:self.context_failures-=1;raise APIError(503,'injected context failure')
   return {}
  if method=='emit':
   if self.finish_failures:self.finish_failures-=1;raise APIError(503,'injected terminal write failure')
   s=self.turns[a['tid']]
   if s['status']!='PROCESSING':return False
   if a['kind']=='turn.finished':
    s.update(a['payload']);s['lastEventId']+=1;self.finished.append(a['payload']['status'])
    if self.lost_finish_acks:self.lost_finish_acks-=1;raise APIError(503,'injected lost terminal acknowledgment')
   return True
  raise AssertionError(method)
 def snapshot(self,tid):return {k:copy.deepcopy(v) for k,v in self.turns[tid].items() if k not in {'cid','requested_model','recipient_context'}}
 async def generate(self,cid,tid,receipt):
  self.generation_calls+=1;self.run_entered.set();await self.run_release.wait()


@pytest.fixture
async def setup(monkeypatch):
 monkeypatch.setenv('OPENROUTER_API_KEY','test-only-not-a-real-key')
 store=FaultStore();runtime=Runtime(store,object(),'test-owner');runtime.generate=store.generate
 monkeypatch.setattr(main.app.state,'store',store,raising=False)
 monkeypatch.setattr(main.app.state,'runtime',runtime,raising=False)
 async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='https://test',headers={'Authorization':'Bearer '+main.TOKEN}) as client:
  yield store,runtime,client
 await runtime.close()


def turn_path():
 return f'/v1/chat/conversations/{uuid.uuid4()}/turns/{uuid.uuid4()}'


async def wait_finished(runtime):
 await asyncio.gather(*list(runtime.jobs.values()))
 await asyncio.sleep(0)


async def test_lost_start_ack_is_recovered_by_exactly_one_owned_job(setup):
 store,runtime,client=setup;store.lost_start_acks=1;path=turn_path()
 response=await client.put(path,json={'message':'테스트'})
 assert response.status_code==202
 assert store.starts==2 and store.generation_calls==1 and runtime.healthy
 store.run_release.set();await wait_finished(runtime)
 assert store.finished==['COMPLETED']
 # A subsequent identical request reports its terminal state without regeneration.
 response=await client.put(path,json={'message':'테스트'})
 assert response.status_code==200 and response.json()['data']['status']=='COMPLETED'
 assert store.generation_calls==1


async def test_concurrent_duplicate_and_conflict_keep_one_owner(setup):
 store,runtime,client=setup;path=turn_path();store.start_release.clear()
 first=asyncio.create_task(client.put(path,json={'message':'동일 질문'}))
 await store.start_entered.wait()
 second=asyncio.create_task(client.put(path,json={'message':'동일 질문'}))
 conflict=await client.put(path,json={'message':'다른 질문'})
 assert conflict.status_code==409
 store.start_release.set();responses=await asyncio.gather(first,second)
 assert [r.status_code for r in responses]==[202,202]
 assert store.starts==1 and store.generation_calls==1
 store.run_release.set();await wait_finished(runtime)


async def test_http_request_cancellation_does_not_cancel_owned_admission(setup):
 store,runtime,client=setup;path=turn_path();store.start_release.clear()
 request=asyncio.create_task(client.put(path,json={'message':'연결 종료'}))
 await store.start_entered.wait();request.cancel()
 with pytest.raises(asyncio.CancelledError):await request
 assert len(runtime.jobs)==1 and not next(iter(runtime.jobs.values())).cancelled()
 store.start_release.set();await store.run_entered.wait()
 store.run_release.set();await wait_finished(runtime)
 assert store.generation_calls==1 and store.finished==['COMPLETED']


async def test_cancel_during_admission_cannot_generate_late_or_pause_other_jobs(setup):
 store,runtime,client=setup;path=turn_path();store.start_release.clear()
 request=asyncio.create_task(client.put(path,json={'message':'취소'}))
 await store.start_entered.wait()
 response=await client.put(path+'/cancellation')
 assert response.status_code==200 and response.json()['data']['status']=='CANCELLED'
 assert (await request).status_code==503
 store.start_release.set();await wait_finished(runtime)
 assert store.generation_calls==0 and store.finished==['CANCELLED'] and runtime.healthy
 retry=await client.put(path,json={'message':'취소'})
 assert retry.status_code==200 and retry.json()['data']['status']=='CANCELLED'
 assert store.generation_calls==0


async def test_cancel_active_turn_is_terminal_and_idempotent(setup):
 store,runtime,client=setup;path=turn_path()
 assert (await client.put(path,json={'message':'취소'})).status_code==202
 a=await client.put(path+'/cancellation');b=await client.put(path+'/cancellation')
 assert a.status_code==b.status_code==200
 assert a.json()['data']==b.json()['data']
 await wait_finished(runtime)
 assert store.finished==['CANCELLED'] and runtime.healthy


@pytest.mark.parametrize('failed',[False,True])
async def test_context_failure_does_not_prevent_terminal_write(setup,failed):
 store,runtime,client=setup;store.context_failures=99;store.run_release.set()
 if failed:
  async def fail(*args):raise ValueError('injected model failure')
  runtime.generate=fail
 await client.put(turn_path(),json={'message':'기록 장애'})
 await wait_finished(runtime)
 assert store.context_calls==2
 assert store.finished==['FAILED' if failed else 'COMPLETED']
 assert runtime.healthy


async def test_lost_terminal_ack_is_retried_without_duplicate_event(setup):
 store,runtime,client=setup;store.lost_finish_acks=1;store.run_release.set()
 await client.put(turn_path(),json={'message':'종료 복구'})
 await wait_finished(runtime)
 assert store.finished==['COMPLETED'] and runtime.healthy


@pytest.mark.parametrize('failure',['admission','finish'])
async def test_exhausted_state_write_retries_pause_admission_for_lease_recovery(setup,failure):
 store,runtime,client=setup;store.run_release.set()
 if failure=='admission':store.start_failures=99
 else:store.finish_failures=99
 response=await client.put(turn_path(),json={'message':'저장 장애'})
 await wait_finished(runtime)
 assert not runtime.healthy
 assert (await client.put(turn_path(),json={'message':'새 질문'})).status_code==503
 if failure=='admission':assert response.status_code==503 and store.starts==3


async def test_sse_notification_during_yield_is_not_delayed_by_polling(monkeypatch):
 tid=uuid.uuid4()
 class Store:
  reads=0
  async def call(self,method,**args):
   if method=='binding':return {'protocolFixture':False}
   self.reads+=1
   state={'turnId':str(tid),'message':'합성 질문','blocks':[],
          'status':'PROCESSING' if self.reads==1 else 'COMPLETED','lastEventId':self.reads}
   if self.reads==1:return [[{'id':1,'event':'progress','data':{'message':'시작'}}],state]
   return [[{'id':2,'event':'turn.finished','data':state}],state]
 class Request:
  url=SimpleNamespace(path=main.BASE)
  query_params={}
  async def is_disconnected(self):return False
 monkeypatch.setattr(main.app.state,'store',Store(),raising=False)
 monkeypatch.setattr(main.app.state,'runtime',SimpleNamespace(signals={}),raising=False)
 response=await main.events(uuid.uuid4(),tid,Request(),None,0)
 iterator=response.body_iterator;await iterator.__anext__()
 main.app.state.runtime.signals[str(tid)].set()
 frame=await asyncio.wait_for(iterator.__anext__(),timeout=0.1)
 assert 'id: 2' in frame
 await iterator.aclose()


async def test_login_store_outage_preserves_identity_cookie(monkeypatch):
 class Store:
  calls=[]
  async def call(self,method,**args):
   self.calls.append(args);raise APIError(503,'temporary store outage')
 store=Store();monkeypatch.setattr(main.app.state,'store',store,raising=False)
 monkeypatch.setattr(main.app.state,'runtime',SimpleNamespace(model='test'),raising=False)
 monkeypatch.setenv('DEMO_ACCESS_TOKEN','test-team-token')
 sid=str(uuid.uuid4());cookie=SIGNER.dumps(sid,salt='demo')
 async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='https://test') as client:
  client.cookies.set('chat_session',cookie,domain='test.local',path='/')
  response=await client.post('/demo/session',json={'token':'test-team-token'})
  assert response.status_code==503 and 'set-cookie' not in response.headers
  assert client.cookies.get('chat_session')==cookie
 assert store.calls==[{'sid':sid}]


async def test_definitive_conflict_after_transient_failure_does_not_pause_others(setup):
 store,runtime,client=setup;path=turn_path();cid,tid=path.split('/')[-3],path.split('/')[-1]
 store.turns[tid]={'turnId':tid,'status':'COMPLETED','message':'원래 질문','blocks':[],'lastEventId':1,'cid':cid}
 store.start_failures=1
 response=await client.put(path,json={'message':'다른 질문'})
 assert response.status_code==409 and runtime.healthy
 assert store.generation_calls==0


async def test_unowned_processing_turn_is_recovered_instead_of_regenerated(setup):
 store,runtime,client=setup;path=turn_path();cid,tid=path.split('/')[-3],path.split('/')[-1]
 store.turns[tid]={'turnId':tid,'status':'PROCESSING','message':'이전 질문','blocks':[],'lastEventId':0,'cid':cid}
 response=await client.put(path,json={'message':'이전 질문'})
 assert response.status_code==503 and not runtime.healthy
 assert runtime.recovery_version>runtime.recovered_version and store.generation_calls==0


async def test_heartbeat_cannot_erase_recovery_requested_during_lease_response(monkeypatch):
 monkeypatch.setenv('OPENROUTER_API_KEY','test-only-not-a-real-key')
 monkeypatch.setenv('STORE_MODE','http');monkeypatch.setenv('CHAT_AGENT_MODE','baseline')
 monkeypatch.setenv('STORE_URL','https://invalid.test');monkeypatch.setenv('STORE_TOKEN','test')
 monkeypatch.setenv('SEARCH_TOKEN','test');monkeypatch.setenv('DEMO_ACCESS_TOKEN','test')
 observed=asyncio.Event()
 class Store:
  async def call(self,method,**args):
   assert method=='lease'
   if args.get('inspect'):return {'owner':None,'active':False}
   if not args.get('release'):
    runtime.require_recovery();observed.set()
   return True
  async def close(self):pass
 class Search:
  async def close(self):pass
 store=Store();search=Search();runtime=Runtime(store,search,'test-owner')
 monkeypatch.setattr(main,'RemoteStore',lambda *args:store)
 monkeypatch.setattr(main,'SearchClient',lambda *args,**kwargs:search)
 monkeypatch.setattr(main,'LowLatencyRuntime',lambda *args:runtime)
 async with main.lifespan(main.app):
  await asyncio.wait_for(observed.wait(),timeout=1)
  assert not runtime.healthy
  assert runtime.recovery_version>runtime.recovered_version


async def test_lost_lease_rotation_ack_reuses_same_cas_pair(monkeypatch):
 monkeypatch.setenv('OPENROUTER_API_KEY','test-only-not-a-real-key')
 monkeypatch.setenv('STORE_MODE','http');monkeypatch.setenv('CHAT_AGENT_MODE','baseline')
 monkeypatch.setenv('STORE_URL','https://invalid.test');monkeypatch.setenv('STORE_TOKEN','test')
 monkeypatch.setenv('SEARCH_TOKEN','test');monkeypatch.setenv('DEMO_ACCESS_TOKEN','test')
 confirmed=asyncio.Event()
 class Store:
  def __init__(self):self.rotations=[];self.owner=None
  async def call(self,method,**args):
   assert method=='lease'
   if args.get('inspect'):return {'owner':self.owner,'active':self.owner is not None}
   if args.get('release'):return True
   self.rotations.append(args.copy())
   if len(self.rotations)==1:
    self.owner=args['next_owner']
    raise APIError(503,'rotation committed but acknowledgment lost')
   assert args['next_owner']==self.owner
   confirmed.set();return True
  async def close(self):pass
 class Search:
  async def close(self):pass
 store=Store();search=Search();runtime=Runtime(store,search,'old-owner')
 monkeypatch.setattr(main,'RemoteStore',lambda *args:store)
 monkeypatch.setattr(main,'SearchClient',lambda *args,**kwargs:search)
 monkeypatch.setattr(main,'LowLatencyRuntime',lambda *args:runtime)
 async with main.lifespan(main.app):
  await asyncio.wait_for(confirmed.wait(),timeout=3)
  assert store.rotations[0]==store.rotations[1]
  assert runtime.owner==store.owner and runtime.healthy
