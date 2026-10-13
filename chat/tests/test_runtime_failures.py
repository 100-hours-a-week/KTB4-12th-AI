import asyncio,os
import httpx,pytest
from app.runtime import Runtime

class Store:
 def __init__(self):self.events=[];self.context=None
 async def call(self,method,**args):
  if method=='emit':self.events.append((args['kind'],args['payload']));return True
  if method=='binding':return {'teamSession':False,'recipientUserId':42}
  if method=='read':return {'turns':[{'turnId':'turn','message':'추천해줘','blocks':[],'status':'PROCESSING'}]}
  if method=='context':self.context=args.get('value');return {}
class Search:
 async def metadata(self):return {'categories':[]}
 async def search(self,*a,**k):raise httpx.ConnectTimeout('test timeout')
@pytest.fixture
async def runtime(monkeypatch):
 monkeypatch.setenv('OPENROUTER_API_KEY','test-only')
 store=Store();r=Runtime(store,Search(),'owner')
 yield r,store
 await r.close()
async def test_search_outage_is_failure_not_empty_success(runtime):
 r,s=runtime
 async def plan(*args,**kwargs):return {'search':True,'query':'텀블러','filters':{}}
 r.complete=plan
 await r.run('conversation','turn')
 assert s.events[-1]==('turn.finished',{'status':'FAILED','error':{'message':'검색 또는 답변 생성에 실패했어요. 잠시 후 다시 시도해 주세요.'}})
 assert not any(k=='cards' for k,_ in s.events)
 assert s.context['failureStage']=='search' and s.context['searchRequest']['query']=='텀블러'
 assert s.context['outcome']=='FAILED'
async def test_llm_429_terminates(runtime):
 r,s=runtime
 async def limited(*a,**kw):
  request=httpx.Request('POST','https://example.test')
  raise httpx.HTTPStatusError('limited',request=request,response=httpx.Response(429,request=request))
 r.complete=limited
 await r.run('conversation','turn')
 assert s.events[-1][1]['status']=='FAILED'
async def test_cancelled_generation_cannot_complete(runtime):
 r,s=runtime;started=asyncio.Event()
 async def wait(*a,**kw):started.set();await asyncio.Event().wait()
 r.generate=wait
 task=asyncio.create_task(r.run('conversation','turn'));await started.wait();task.cancel();await task
 assert not any(k=='turn.finished' for k,_ in s.events)
async def test_structured_retry_is_bounded(runtime):
 from app.runtime import Plan
 r,_=runtime;n=0
 def reply(req):
  nonlocal n;n+=1
  return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"search":'}}]})
 await r.http.aclose();r.http=httpx.AsyncClient(base_url='https://example.test',transport=httpx.MockTransport(reply))
 with pytest.raises(ValueError):await r.complete([],[],Plan)
 assert n==2
async def test_structured_retry_recovers_format(runtime):
 from app.runtime import Plan
 r,_=runtime;n=0
 def reply(req):
  nonlocal n;n+=1
  value='{"search":' if n==1 else '{"search":false,"query":"","filters":{}}'
  return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':value}}]})
 await r.http.aclose();r.http=httpx.AsyncClient(base_url='https://example.test',transport=httpx.MockTransport(reply))
 value=await r.complete([],[],Plan);assert value['search'] is False;assert n==2
