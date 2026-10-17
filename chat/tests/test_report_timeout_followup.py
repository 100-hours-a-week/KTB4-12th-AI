from test_hf_integration import SyntheticPrices
import asyncio,json,time,uuid
from contextlib import contextmanager
import httpx,pytest
from app import low_latency
from app.low_latency import LowLatencyRuntime
from app.model_config import ModelCatalog
from app.task_budget import BudgetHTTP
from app.store import Store as PersistentStore
from app.errors import APIError
from app.turn_report import report
from test_low_latency import Store,Search,text,tool,context

class DelayedStream(httpx.AsyncByteStream):
 def __init__(self,delay,packet,started,error=None):self.delay,self.packet,self.started,self.error=delay,packet,started,error
 async def __aiter__(self):
  self.started.set();await asyncio.sleep(self.delay)
  if self.error:raise self.error
  yield ('data: '+json.dumps(self.packet)+'\n\ndata: [DONE]\n\n').encode()

async def delayed_turn(delays,timeout=.15,error=None,cancel=False):
 store=Store();search=Search();cid,tid=str(uuid.uuid4()),str(uuid.uuid4());sent=[];started=asyncio.Event()
 await store.call('conversation',cid=cid,sender=7,recipient=42,protocol_fixture=False,demo_profile=None)
 await store.call('start',cid=cid,tid=tid,message='합성 지연 테스트 질문')
 async def reply(req):
  index=len(sent);sent.append(json.loads(req.content))
  packet=tool('gift_context',context()) if len(delays)>1 and index==0 else text('합성 답변입니다.')
  return httpx.Response(200,stream=DelayedStream(delays[index],packet,started,error if index==len(delays)-1 else None))
 async with httpx.AsyncClient(base_url='https://synthetic.invalid',transport=httpx.MockTransport(reply)) as http:
  runtime=object.__new__(LowLatencyRuntime);runtime.init_state(store,search,'owner');runtime.model='openai/gpt-6-luna';runtime.catalog=ModelCatalog(http);runtime.http=BudgetHTTP(http,store);runtime.admissions[tid]={};runtime.TURN_TIMEOUT_SECONDS=timeout
  task=asyncio.create_task(runtime.run(cid,tid))
  if cancel:await started.wait();task.cancel()
  await task
 return store,store.traces[tid],sent,cid,tid

async def test_legacy_loop_deadline_reproduces_early_abort_and_unknown_budget(monkeypatch):
 original=LowLatencyRuntime.harness_class(None)
 def legacy(*args,**kw):kw['turn_timeout']=.02;return original(*args,**kw)
 monkeypatch.setattr(LowLatencyRuntime,'harness_class',lambda self:legacy)
 store,receipt,sent,cid,tid=await delayed_turn([.04])
 assert store.states[tid]['status']=='FAILED' and receipt['agentFailureCode']=='TimeoutError'
 assert receipt['agentFailureStage']=='model_1' and receipt['stages']['model_1_ms']>=15
 assert len(sent)==1 and store.budget[-1][1]['cost'] is None and store.budget[-1][1]['status']=='unknown'

async def test_production_outer_deadline_accepts_packet_after_legacy_deadline():
 store,receipt,sent,cid,tid=await delayed_turn([.04])
 assert store.states[tid]['status']=='COMPLETED' and len(sent)==1
 assert receipt['stages']['model_1_ms']>=35 and store.budget[-1][1]['cost']==.001

async def test_outer_turn_deadline_still_bounds_generation_and_keeps_unknown():
 store,receipt,sent,cid,tid=await delayed_turn([.06],timeout=.025)
 assert store.states[tid]['status']=='FAILED' and receipt['failure']=='TimeoutError'
 assert receipt['agentFailureStage']=='model_1' and len(sent)==1
 assert store.budget[-1][1]['cost'] is None and store.budget[-1][1]['status']=='unknown'
 out=report(cid,store.public(store.states[tid]),receipt)
 assert out['diagnosticCode']=='turn_deadline_exceeded' and out['failureStage']=='model_1' and out['metrics']['stages']['model_1_ms']>=20 and out['metrics']['costUsd'] is None

async def test_provider_read_timeout_remains_failure_without_repair():
 store,receipt,sent,cid,tid=await delayed_turn([.01],error=httpx.ReadTimeout('synthetic transport stall'))
 assert store.states[tid]['status']=='FAILED' and receipt['agentFailureCode']=='ReadTimeout'
 assert receipt['agentFailureStage']=='model_1' and len(sent)==1 and not search_calls(receipt)
 assert store.budget[-1][1]['cost'] is None

def search_calls(receipt):return receipt.get('searchBranches',0)

async def test_second_round_deadline_keeps_first_usage_and_second_unknown():
 store,receipt,sent,cid,tid=await delayed_turn([.001,.06],timeout=.03)
 assert store.states[tid]['status']=='FAILED' and len(sent)==2
 assert receipt['agentFailureStage']=='model_2' and set(('model_1_ms','model_2_ms'))<=set(receipt['stages'])
 finishes=[args for method,args in store.budget if method=='budget_finish']
 assert len(finishes)==2 and finishes[0]['cost']==.001 and finishes[1]['cost'] is None
 out=report(cid,store.public(store.states[tid]),receipt)
 assert out['metrics']['usage'][0]['cost']==.001 and out['metrics']['costUsd'] is None

async def test_user_cancel_preserves_unknown_and_cannot_commit_completed():
 store,receipt,sent,cid,tid=await delayed_turn([.06],cancel=True)
 assert receipt['outcome']=='INTERRUPTED' and store.states[tid]['status']!='COMPLETED'
 assert len(sent)==1 and store.budget[-1][1]['cost'] is None

class Result:
 def __init__(self,row=None,rows=()):self.row,self.rows=row,rows
 def fetchone(self):return self.row
 def fetchall(self):return self.rows
class DB:
 def __init__(self,old=None):self.old=old;self.inserts=0
 def execute(self,sql,args):
  if sql.startswith('SELECT turn,payload FROM feedback'):return Result(self.old)
  if sql.startswith('SELECT state,context FROM turns'):return Result(rows=[{'state':{'turnId':'t','status':'FAILED','message':'合成','blocks':[],'lastEventId':1},'context':{}}])
  if sql.startswith('INSERT INTO feedback'):
   self.inserts+=1;self.old={'turn':args[1],'payload':args[2].obj}
  return Result()
class Pool:
 def __init__(self,db):self.db=db
 @contextmanager
 def connection(self):yield self.db
def reporting_store(old=None):
 db=DB(old);store=PersistentStore.__new__(PersistentStore);store.pool=Pool(db);store._turn=lambda *a:{'ordinal':1,'state':{'status':'FAILED'}}
 return store,db

@pytest.mark.parametrize('verdict,note',[('issue',''),('unhelpful','  '),('issue','a'*1501)])
def test_new_issue_requires_specific_note_before_insert(verdict,note):
 store,db=reporting_store()
 with pytest.raises(APIError) as exc:store.feedback('c','t','f',{'verdict':verdict,'note':note})
 assert exc.value.status==400 and db.inserts==0

def test_valid_issue_is_saved_and_idempotent():
 store,db=reporting_store();payload={'verdict':'issue','note':'합성 지연으로 답변을 받지 못함'}
 assert store.feedback('c','t','f',payload)=={'id':'f','saved':True}
 assert store.feedback('c','t','f',payload)=={'id':'f','saved':True} and db.inserts==1

def test_legacy_empty_note_retry_preserved_and_changed_body_conflicts():
 payload={'verdict':'issue','note':''};store,db=reporting_store({'turn':'t','payload':payload})
 assert store.feedback('c','t','f',payload)['saved'] and db.inserts==0
 with pytest.raises(APIError) as exc:store.feedback('c','t','f',{'verdict':'issue','note':'새 설명'})
 assert exc.value.status==409 and db.inserts==0

def test_helpful_legacy_qa_can_remain_note_optional():
 store,db=reporting_store();assert store.feedback('c','t','f',{'verdict':'helpful','note':''})['saved'] and db.inserts==1
