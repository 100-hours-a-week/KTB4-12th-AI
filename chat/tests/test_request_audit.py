import copy,json,os
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from uuid import uuid4
import httpx,pytest
from app.request_audit import capture_request,trace,conversation_projection,usage,text
from app.llm_connection import ACTIVE_CONNECTION,AdmittedConnection
from app import main,security

NOW=datetime.now(timezone.utc).isoformat()
def state():return {'turnId':str(uuid4()),'status':'COMPLETED','message':'합성 공개 질문','blocks':[{'type':'cards','products':[{'productId':101,'reason':'공개 상품의 용도와 맞습니다.'}]}],'lastEventId':3}
def context():return {'sourceScope':'synthetic-local','environment':'local','revision':'e7383488','modelRequests':[{}],'requestAudit':[{'index':1,'startedAt':NOW,'offsetMs':1,'status':'completed','input':{'capture':'actual-send-time-redacted'},'usage':{'prompt_tokens':100,'completion_tokens':20,'cost':.001,'prompt_tokens_details':{'cached_tokens':80,'cache_write_tokens':10},'completion_tokens_details':{'reasoning_tokens':5}},'toolNames':['show_gifts']}],'usage':[],'diagnostics':{'operations':[{'name':'catalog.search','startedAt':NOW,'turnOffsetMs':2,'status':'completed','arguments':{'query':'PRIVATE_QUERY'},'result':{'products':[{'productId':101}]}}]}}
def test_capture_discards_all_private_or_unknown_state_before_persistence():
 raw={'model':'synthetic','messages':[{'role':'system','content':'Policy\nSTATE_JSON\n'+json.dumps({'filters':{},'recipient_preferences':{'giftPreference':'PRIVATE_CANARY'},'cached_recipient':{'preferences':'ANOTHER_PRIVATE'},'unknown':{'secret':'PRIVATE_THIRD'}})},{'role':'assistant','tool_calls':[{'function':{'name':'gift_context','arguments':'PRIVATE_QUERY'}}]},{'role':'tool','content':json.dumps({'products':[{'product_id':'101','description':'公开 catalog'}],'queries':['PRIVATE_QUERY']})}],'tools':[]}
 recorded=capture_request(raw);value=json.dumps(recorded)
 assert not any(v in value for v in ('PRIVATE_CANARY','ANOTHER_PRIVATE','PRIVATE_THIRD','PRIVATE_QUERY'))
 assert '公开 catalog' in json.dumps(recorded,ensure_ascii=False)
def test_bound_capture_and_literal_text_no_html_execution():
 r=capture_request({'messages':[{'role':'user','content':'x'*5000} for _ in range(32)]})
 assert r['truncated'] and len(json.dumps(r))<24000
 assert text('<img src=x onerror=bad()>')=='<img src=x onerror=bad()>'
def test_totals_never_double_add_cache_or_reasoning():
 t=trace('cid',state(),context());assert t['totals']['inputTokens']==100 and t['totals']['outputTokens']==20
 u=t['modelCalls'][0]['usage'];assert u['cachedReadTokens']==80 and u['reasoningTokens']==5
 assert 'PRIVATE_QUERY' not in json.dumps(t)
@pytest.mark.parametrize('field',['prompt_tokens_details','completion_tokens_details'])
def test_malformed_usage_details_are_unknown_not_view_failure(field):
 c=context();c['requestAudit'][0]['usage'][field]=['invalid'];assert trace('cid',state(),c)['totals']['inputTokens']==100
 assert usage({'prompt_tokens_details':['invalid']})['cachedReadTokens'] is None
def test_legacy_and_missing_receipt_not_fabricated():
 t=trace('cid',state(),{});assert t['recording']['calls']=='not-recorded-legacy' and t['modelCalls']==[] and t['totals']['costUsd'] is None
 c=context();c['modelRequests'].append({});assert trace('cid',state(),c)['totals']['inputTokens'] is None
 c['requestAudit'][0].pop('usage');assert trace('cid',state(),c)['totals']['costUsd'] is None
def test_details_expire_without_claiming_persisted_data_deletion():
 old=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat();t=trace('cid',state(),context(),details=True,created=old)
 assert 'publicInput' not in t and 'input' not in t['modelCalls'][0]
 assert t['recording']['storedRecordRetention']=='existing Chat store policy; no new deletion job'
def test_default_projection_masks_prose_and_retains_pagination():
 t=state();data={'conversationId':'cid','turns':[{'turnId':t['turnId'],'state':t,'context':context(),'created':NOW,'feedback':[],'reviews':[]}],'nextCursor':20}
 out=conversation_projection(data);assert out['nextCursor']==20 and out['turns'][0]['state']['blocks']==[]
 assert '합성 공개 질문' not in json.dumps(out,ensure_ascii=False);assert data['turns'][0]['state']['message']=='합성 공개 질문'
async def test_arbitrarily_named_registered_secret_is_redacted():
 client=httpx.AsyncClient(headers={'Authorization':'Bearer private-credential-with-neutral-env-name'})
 conn=AdmittedConnection('openai-compatible','http://127.0.0.1/v1','mock',1,client)
 token=ACTIVE_CONNECTION.set(conn)
 try:assert text('echo private-credential-with-neutral-env-name')=='echo [redacted]'
 finally:ACTIVE_CONNECTION.reset(token);await client.aclose()
async def test_trace_route_requires_admin_and_exact_binding(monkeypatch):
 tid=str(uuid4());cid=str(uuid4());turn={**state(),'turnId':tid}
 class Store:
  async def call(self,method,**args):
   assert method=='admin_conversation' and args['cid']==cid and args['focus_tid']==tid
   return {'conversationId':cid,'turns':[{'turnId':tid,'state':turn,'context':context(),'created':NOW}]}
 monkeypatch.setattr(main.app.state,'store',Store(),raising=False);security.rate.clear()
 async with httpx.AsyncClient(base_url='https://synthetic',transport=httpx.ASGITransport(app=main.app)) as client:
  path=f'/admin/api/conversations/{cid}/turns/{tid}/trace?details=true'
  assert (await client.get(path)).status_code==401
  assert (await client.post('/admin/login',json={'password':os.environ['QA_ADMIN_PASSWORD']})).status_code==200
  response=await client.get(path);assert response.status_code==200 and response.json()['publicInput']=='합성 공개 질문'
  assert response.headers['cache-control']=='no-store, no-transform'
  assert (await client.get(path,headers={'Origin':'https://evil.invalid'})).status_code==403

@pytest.mark.parametrize('receipt',[
 {'prompt_tokens':100,'completion_tokens':20,'cost':.001},
 {'prompt_tokens':0,'completion_tokens':0,'cost':0},
 {'prompt_tokens':100,'completion_tokens':20},
 {'prompt_tokens':True,'completion_tokens':20,'cost':.001},
 {'prompt_tokens':-1,'completion_tokens':20,'cost':.001},
 {'prompt_tokens':100,'completion_tokens':None,'cost':.001},
 {'prompt_tokens':100,'completion_tokens':20,'cost':float('nan')},
 None,
])
@pytest.mark.parametrize('expected',[1,2])
def test_list_and_detail_share_complete_receipt_totals(receipt,expected):
 from app.request_audit import list_summary
 ctx=context();ctx['modelRequests']=[{}]*expected;ctx['requestAudit'][0]['usage']=receipt
 detailed=trace('cid',state(),ctx)['totals']
 summary=list_summary({'usage':[receipt],'attemptCount':expected})
 for key in ('inputTokens','outputTokens','costUsd','usageState'):
  assert summary[key]==detailed[key]

def test_list_requires_attempt_count_but_legacy_detail_keeps_observed_receipts():
 from app.request_audit import list_summary
 ctx=context();ctx.pop('modelRequests')
 assert trace('cid',state(),ctx)['totals']['inputTokens']==100
 assert list_summary({'usage':[ctx['requestAudit'][0]['usage']]})['inputTokens'] is None
