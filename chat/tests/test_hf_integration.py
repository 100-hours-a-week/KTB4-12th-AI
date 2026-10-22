import asyncio,copy,json,uuid
from contextlib import asynccontextmanager
import httpx,pytest
from app.turn_report import report,text_report
from app.errors import APIError
from app.protocol_fixture import ProtocolFixture
from app.runtime import Runtime
from app import main,integration_client

class SyntheticPrices:
 # Fixed quotes belong only to offline fixtures, never production policy.
 async def resolve(self,body):
  return {'prompt':'0.00000099' if body['model']=='qwen/qwen3.8-27b' else '0.0000006',
          'completion':'0.00000149' if body['model']=='qwen/qwen3.8-27b' else '0.000005','request':'0'}





@pytest.mark.parametrize('mode',['live','protocol-fixture'])
def test_public_report_excludes_private_trace_and_handles_unknown_cost(mode):
 turn={'turnId':'t','status':'COMPLETED','message':'공개 질문','blocks':[{'type':'text','text':'공개 답변'}],'lastEventId':3}
 trace={'model':'m','recipientProfile':{'data':'SECRET'},'plan':{'query':'SECRET'},'failureMessage':'SECRET','usage':[{'cost':None,'prompt_tokens':5,'hidden':'SECRET'}],'selectedProducts':[{'productId':1,'name':'공개 상품','price':3,'reason':'SECRET'}]}
 if mode=='protocol-fixture':trace['externalCalls']=0
 out=report('c',turn,trace)
 assert 'SECRET' not in json.dumps(out) and 'SECRET' not in text_report(out)
 assert out['metrics']['costUsd']==(0 if mode=='protocol-fixture' else None)
 assert out['mode']==mode

def test_standalone_client_preserves_fixture_and_optional_body():
 s={'conversationId':'c','message':'m','protocolFixture':True,'model':'vendor/model','recipientContext':{'recipientUserId':9}}
 assert integration_client.root_path(s)=='/v1/chat/test/conversations/c'
 assert integration_client.request_body(s)=={'message':'m','model':'vendor/model','recipientContext':{'recipientUserId':9}}

class FixtureStore:
 def __init__(self):self.convs={};self.states={};self.traces={};self.events={};self.sessions={};self.feedbacks={}
 async def call(self,method,**a):
  cid=a.get('cid');tid=a.get('tid')
  if method=='conversation':
   binding={k:a.get(k) for k in ('sender','recipient','protocol_fixture','demo_profile')}
   if cid in self.convs:
    if self.convs[cid]!=binding:raise APIError(409,'binding')
    made=False
   else:self.convs[cid]=binding;made=True
   return [{'conversationId':cid,'turns':[copy.deepcopy(t) for t in self.states.values() if t.get('_cid')==cid]},made]
  if method=='binding':
   if cid not in self.convs:raise APIError(404,'conversation')
   return {'protocolFixture':bool(self.convs[cid]['protocol_fixture'])}
  if method=='existing_turn':
   t=self.states.get(tid)
   if t is not None and (t['_cid']!=cid or t['message']!=a['message']):raise APIError(409,'fingerprint')
   return self.public(t) if t else None
  if method=='start':
   if tid in self.states:return [self.public(self.states[tid]),False]
   if any(t['_cid']==cid and t['status']=='PROCESSING' for t in self.states.values()):raise APIError(409,'active')
   t={'turnId':tid,'status':'PROCESSING','message':a['message'],'blocks':[],'lastEventId':0,'_cid':cid};self.states[tid]=t;self.events[tid]=[]
   return [self.public(t),True]
  if method=='read':return {'conversationId':cid,'turns':[self.public(t) for t in self.states.values() if t['_cid']==cid]}
  if method=='recipient_context':return None
  if method=='context':
   if 'value' in a:self.traces[tid]=copy.deepcopy(a['value'])
   return copy.deepcopy(self.traces.get(tid,{}))
  if method in ('emit','replay'):
   if tid not in self.states or self.states[tid]['_cid']!=cid:raise APIError(404,'turn')
   t=self.states[tid]
   if method=='replay':return [copy.deepcopy([e for e in self.events[tid] if e['id']>a['after']]),self.public(t)]
   if t['status']!='PROCESSING':return False
   kind,payload=a['kind'],a['payload'];t['lastEventId']+=1
   if kind=='answer.delta':
    if t['blocks'] and t['blocks'][-1]['type']=='text':t['blocks'][-1]['text']+=payload['delta']
    else:t['blocks'].append({'type':'text','text':payload['delta']})
   elif kind=='cards':t['blocks'].append({'type':'cards','products':payload['products']})
   elif kind=='turn.finished':t.update(payload);t.pop('progressMessage',None)
   else:t['progressMessage']=payload['message']
   self.events[tid].append({'id':t['lastEventId'],'event':kind,'data':self.public(t) if kind=='turn.finished' else payload});return True
  if method=='session':
   if 'sender' in a:self.sessions[a['sid']]=a['sender']
   if a['sid'] not in self.sessions:raise APIError(401,'session')
   return self.sessions[a['sid']]
  if method=='owned':
   if cid not in self.convs or self.convs[cid]['sender']!=a['sender']:raise APIError(404,'owner')
   return True
  if method=='list_conversations':return []
  if method=='own_feedback':return {'reports':[]}
  raise AssertionError(method)
 def public(self,t):return {k:copy.deepcopy(v) for k,v in t.items() if not k.startswith('_')}

@pytest.fixture
async def fixture_app(monkeypatch):
 store=FixtureStore();primary=object.__new__(Runtime);primary.init_state(store,None,'owner');primary.model='live-unused'
 main.app.state.store=store;main.app.state.runtime=primary;main.app.state.fixture=ProtocolFixture(primary);main.app.state.runtime_mode='live'
 async with httpx.AsyncClient(base_url='https://test',transport=httpx.ASGITransport(app=main.app)) as client:yield client,store,primary
 await primary.stop_jobs()

async def test_authenticated_fixture_lifecycle_retry_replay_cancel_and_mode_isolation(fixture_app):
 client,store,primary=fixture_app;cid,tid=str(uuid.uuid4()),str(uuid.uuid4());root=main.TEST_BASE+'/'+cid;auth={'Authorization':'Bearer '+main.TOKEN}
 assert (await client.put(root,json={'senderUserId':7,'recipientUserId':42})).status_code==401
 assert (await client.put(root,json={'senderUserId':7,'recipientUserId':42},headers=auth)).status_code==201
 assert (await client.put(root+'/turns/'+tid,json={'message':'/mock text','model':'openai/gpt-6-luna'},headers=auth)).status_code==400
 response=await client.put(root+'/turns/'+tid,json={'message':'/mock text'},headers=auth);assert response.status_code==202
 await asyncio.gather(*primary.jobs.values())
 final=(await client.get(root+'/turns/'+tid,headers=auth)).json()['data'];assert final['status']=='COMPLETED'
 retry=await client.put(root+'/turns/'+tid,json={'message':'/mock text'},headers=auth);assert retry.status_code==200 and retry.json()['data']==final
 assert (await client.put(root+'/turns/'+tid,json={'message':'changed'},headers=auth)).status_code==409
 frames=(await client.get(root+'/turns/'+tid+'/events?afterEventId=1',headers=auth)).text
 assert 'id: 1\n' not in frames and 'event: turn.finished' in frames
 assert (await client.put(root+'/turns/'+tid+'/cancellation',headers=auth)).json()['data']==final
 assert (await client.get(main.BASE+'/'+cid,headers=auth)).status_code==409
 report=(await client.get(root+'/turns/'+tid+'/report',headers=auth)).json();assert report['mode']=='protocol-fixture' and report['metrics']['costUsd']==0
 assert len(store.states)==1 and store.traces[tid]['externalCalls']==0

async def test_fixture_cancellation_and_browser_report_owner(fixture_app,monkeypatch):
 monkeypatch.setenv('DEMO_ACCESS_TOKEN','test-team-token')
 client,store,primary=fixture_app;cid,tid=str(uuid.uuid4()),str(uuid.uuid4());root=main.TEST_BASE+'/'+cid;auth={'Authorization':'Bearer '+main.TOKEN}
 await client.put(root,json={'senderUserId':7,'recipientUserId':42},headers=auth)
 await client.put(root+'/turns/'+tid,json={'message':'/mock slow'},headers=auth)
 result=await client.put(root+'/turns/'+tid+'/cancellation',headers=auth);assert result.json()['data']['status']=='CANCELLED'
 assert (await client.get('/demo/conversations/'+cid+'/turns/'+tid+'/report',headers=auth)).status_code==401
 await client.post('/demo/session',json={'token':'test-team-token'})
 assert (await client.get('/demo/conversations/'+cid+'/turns/'+tid+'/report')).status_code==404
