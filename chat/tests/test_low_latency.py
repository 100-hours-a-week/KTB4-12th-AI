from test_hf_integration import SyntheticPrices
import asyncio,copy,json,time,uuid
import httpx,pytest
from app.low_latency import LowLatencyRuntime
from app.model_config import ModelCatalog
from app.task_budget import BudgetHTTP
from app.gift_core.contracts import Filters,strict_schema,ContextArgs
from app.turn_report import report
from test_hf_integration import FixtureStore

META={'snapshotId':'synthetic-v1','categories':[{'id':10,'name':'생활','group':'생활','parentId':1},{'id':20,'name':'식품','group':'식품','parentId':2}]}
PRODUCT={'productId':101,'name':'합성 책받침','brand':'합성','price':10000,'category':'생활','categoryId':10,'description':'책을 올려 두는 합성 테스트 상품','productType':'Shipping','availability':'AVAILABLE'}
class Search:
 def __init__(self):self.queries=[];self.hydrations=[]
 async def metadata(self):return META
 async def search(self,body,snapshot_id):
  self.queries.append(copy.deepcopy(body));assert snapshot_id==META['snapshotId']
  return {'snapshotId':snapshot_id,'hits':[PRODUCT]}
 async def get_products(self,ids,snapshot_id):
  self.hydrations.append(ids);return {'snapshotId':snapshot_id,'products':[PRODUCT] if 101 in ids else []}
class Store(FixtureStore):
 def __init__(self):super().__init__();self.supplied=None;self.budget=[]
 async def call(self,method,**a):
  if method=='recipient_context':return self.supplied
  if method=='binding':return {'recipientUserId':42,'teamSession':False,'demoProfile':None}
  if method.startswith('budget_'):self.budget.append((method,a));return True
  if method=='existing_turn' and a['tid'] in self.states:
   old=self.states[a['tid']]
   if old.get('_prepared')!=a.get('prepared_context'):raise __import__('app.errors',fromlist=['APIError']).APIError(409,'prepared')
  out=await super().call(method,**a)
  if method=='start':self.states[a['tid']]['_prepared']=a.get('prepared_context')
  return out

def text(value):return {'choices':[{'delta':{'content':value},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':10,'cost':.001}}
def tool(name,args):return {'choices':[{'delta':{'tool_calls':[{'index':0,'id':'synthetic-call','function':{'name':name,'arguments':json.dumps(args,ensure_ascii=False)}}]},'finish_reason':'tool_calls'}],'usage':{'prompt_tokens':10,'completion_tokens':10,'cost':.001}}
def context(scope='current',budget=None):return {'queries':['책받침'],'filters':Filters(recipient_scope=scope,budget_action='replace' if budget else 'preserve',max_price=budget).model_dump(),'expand_catalog':False}
def show(final=True):
 out={'message':'자료에서 확인한 후보입니다.','products':[{'product_id':'101','reason':'책을 올려 두는 용도에 맞아요.'}]}
 if not final:out['filters']=Filters().model_dump()
 return out
async def run(packets,prepared=None,private=False,old=None):
 store=Store();search=Search();sent=[];cid,tid=str(uuid.uuid4()),str(uuid.uuid4())
 await store.call('conversation',cid=cid,sender=7,recipient=42,protocol_fixture=False,demo_profile=None)
 if old:
  oid=str(uuid.uuid4());store.states[oid]={'turnId':oid,'status':'COMPLETED','message':'이전 합성 요청','blocks':[],'lastEventId':1,'_cid':cid};store.traces[oid]=old
 if private:store.supplied={'recipientUserId':42,'status':'AVAILABLE','giftPreference':'PRIVATE_CANARY_BOOK_SUPPORT','dislikedCategories':[{'categoryId':20,'categoryName':'식품'}]}
 await store.call('start',cid=cid,tid=tid,message='합성 질문')
 def reply(req):
  sent.append(json.loads(req.content));packet=packets.pop(0)
  return httpx.Response(200,text='data: '+json.dumps(packet,ensure_ascii=False)+'\n\ndata: [DONE]\n\n')
 async with httpx.AsyncClient(base_url='https://synthetic',transport=httpx.MockTransport(reply)) as http:
  runtime=object.__new__(LowLatencyRuntime);runtime.init_state(store,search,'owner');runtime.model='openai/gpt-6-luna';runtime.catalog=ModelCatalog(http);runtime.http=BudgetHTTP(http,store)
  runtime.admissions[tid]={'prepared_context':prepared};receipt={'startedMonotonic':time.monotonic(),'usage':[]}
  await runtime.generate(cid,tid,receipt)
 return receipt,sent,store,search,cid,tid
@pytest.mark.parametrize('prepared,packets,expected',[(None,[text('안녕하세요.')],1),({'productIds':[101],'snapshotId':'synthetic-v1'},[tool('show_gifts',show(False))],1),(None,[tool('gift_context',context(budget=20000)),tool('show_gifts',show())],2)])
async def test_actual_transport_one_one_two_and_public_facts(prepared,packets,expected):
 receipt,sent,store,search,cid,tid=await run(packets,prepared)
 assert len(sent)==len(receipt['modelRequests'])==expected
 assert all(b['reasoning']=={'enabled':True,'effort':'low','exclude':True} and b['max_tokens']==1200 for b in sent)
 assert all(b['provider']['max_price']['completion']==5 for b in sent)
 if expected==2:assert receipt['lowLatencyState']['publicFilters']['max_price']==20000
 if prepared:assert search.hydrations==[[101]] and not search.queries
 if expected==2 or prepared:assert receipt['selectedProducts'][0]['productId']==101
 assert len([x for x in store.budget if x[0]=='budget_reserve'])==expected

async def test_private_profile_and_derived_exclusions_never_enter_model_or_report():
 receipt,sent,store,search,cid,tid=await run([tool('gift_context',context()),tool('show_gifts',show())],private=True)
 wire=json.dumps(sent);assert 'PRIVATE_CANARY' not in wire
 assert all('20' not in json.loads(b['messages'][0]['content'].split('STATE_JSON\n')[1])['filters']['exclude_category_ids'] for b in sent)
 assert receipt['lowLatencyState']['publicFilters']['exclude_category_ids']==[]
 assert search.queries[0]['query']=='PRIVATE_CANARY_BOOK_SUPPORT'
 out=report(cid,store.public(store.states[tid]),receipt);assert 'PRIVATE_CANARY' not in json.dumps(out)

async def test_semantic_recipient_switch_does_not_reuse_private_search_or_exclusions():
 _,_,_,search,_,_=await run([tool('gift_context',context('other')),tool('show_gifts',show())],private=True)
 assert [x['query'] for x in search.queries]==['책받침'] and all(not x['filters']['excludeCategoryIds'] for x in search.queries)

async def test_invalid_terminal_tool_has_no_paid_repair_or_third_request():
 with pytest.raises(Exception):await run([tool('gift_context',context()),tool('show_gifts',{'message':'잘못된 합성 응답','products':[{'product_id':'999','reason':'검증되지 않은 상품'}]})])

async def test_saved_public_facts_followup_needs_one_call():
 receipt,*_=await run([tool('gift_context',context()),tool('show_gifts',show())])
 # No private profile -> recipient identity stable across restarts.
 old={**receipt,'agent':'gift-tool-loop-v1'}
 follow,sent,_,search,_,_=await run([text('이전 상품명은 합성 책받침입니다.')],old=old)
 assert len(sent)==1 and not search.queries
 assert '합성 책받침' in sent[0]['messages'][0]['content']

async def test_prepared_private_exclusion_never_becomes_next_public_filter():
 receipt,sent,_,search,_,_=await run([tool('show_gifts',show(False))],{'productIds':[101],'snapshotId':'synthetic-v1'},private=True)
 assert receipt['lowLatencyState']['publicFilters']['exclude_category_ids']==[]
 assert 'PRIVATE_CANARY' not in json.dumps(sent) and not search.queries

def test_typed_recipient_switch_clears_implicit_prior_budget():
 from app.gift_adapter import CatalogAdapter
 from app.gift_core.contracts import Profile
 catalog=CatalogAdapter(Search(),META,{})
 assert catalog.resolve_filters(Filters(recipient_scope='other'),Filters(max_price=20000),Profile()).max_price is None
 assert catalog.resolve_filters(Filters(recipient_scope='other',budget_action='replace',max_price=20000),Filters(max_price=30000),Profile()).max_price==20000

def test_default_model_selection_reports_effective_low():
 runtime=object.__new__(LowLatencyRuntime);runtime.model='openai/gpt-6-luna';runtime.catalog=ModelCatalog(None)
 assert runtime.effective_config(runtime.catalog.cached(runtime.model)).public()['reasoningLabel']=='추론 low'

def test_parent_exclusion_expands_past_model_limit_without_leaking_private_ids():
 from app.gift_adapter import CatalogAdapter
 from app.gift_core.contracts import Profile
 meta={'snapshotId':'synthetic-large','categories':[{'id':i+100,'name':'합성 분류','group':'합성 그룹','parentId':1} for i in range(26)]}
 catalog=CatalogAdapter(Search(),meta,{})
 resolved=catalog.resolve_filters(Filters(),Filters(),Profile(disliked_category_ids=['1']))
 assert len(resolved.exclude_category_ids)==26 and catalog.public_filters.exclude_category_ids==[]
 resolved=catalog.resolve_filters(Filters(exclude_category_ids=['1']),Filters(),Profile())
 assert len(resolved.exclude_category_ids)==26 and catalog.public_filters.exclude_category_ids==['1']

async def test_model_format_failure_has_only_safe_diagnostic_code():
 from app.low_latency import ProviderAdapter
 from app.model_trace import TRACE
 store=Store();search=Search();cid,tid=str(uuid.uuid4()),str(uuid.uuid4());await store.call('conversation',cid=cid,sender=7,recipient=42,protocol_fixture=False,demo_profile=None);await store.call('start',cid=cid,tid=tid,message='합성 준비 추천')
 payload=tool('show_gifts',{**show(False),'products':[{'product_id':'101','reason':'잘못된 숫자 999 사유'}]})
 async with httpx.AsyncClient(base_url='https://synthetic',transport=httpx.MockTransport(lambda request:httpx.Response(200,text='data: '+json.dumps(payload)+'\n\ndata: [DONE]\n\n'))) as http:
  runtime=object.__new__(LowLatencyRuntime);runtime.init_state(store,search,'owner');runtime.model='openai/gpt-6-luna';runtime.catalog=ModelCatalog(http);runtime.http=BudgetHTTP(http,store);runtime.admissions[tid]={'prepared_context':{'productIds':[101],'snapshotId':'synthetic-v1'}};receipt={'startedMonotonic':time.monotonic(),'usage':[]}
  with pytest.raises(Exception):await runtime.generate(cid,tid,receipt)
 assert receipt['agentFailureCode']=='ValidationError' and len(receipt['modelRequests'])==1
 out=report(cid,store.public(store.states[tid]),receipt)
 assert out['diagnosticCode']=='ValidationError' and out['validationDiagnostics']==[{'location':'products.0.reason','type':'string_pattern_mismatch'}]
 assert '999' not in json.dumps(out)

 # Diagnostics describe the field/type without copying rejected model text.

async def test_successive_fact_followups_keep_verified_selected_facts():
 original,*_=await run([tool('gift_context',context()),tool('show_gifts',show())])
 first,sent,*_=await run([text('첫 상품은 합성 책받침입니다.')],old=original)
 assert first['publicAnswer']['products'][0]['productId']==101
 second,sent,*_=await run([text('그 상품 가격은 만원입니다.')],old=first)
 state=json.loads(sent[0]['messages'][0]['content'].split('STATE_JSON\n')[1])
 assert state['last_products'][0]['product_id']=='101' and len(sent)==1

async def test_new_prepared_pool_cannot_select_previous_outside_pool():
 original,*_=await run([tool('gift_context',context()),tool('show_gifts',show())])
 original['publicAnswer']['products'][0]['productId']=201
 original['lowLatencyState']['lastProducts'][0]['product_id']='201'
 result,sent,*_=await run([tool('show_gifts',show(False))],{'productIds':[101],'snapshotId':'synthetic-v1'},old=original)
 state=json.loads(sent[0]['messages'][0]['content'].split('STATE_JSON\n')[1])
 assert not state['last_products']
 enum=sent[0]['tools'][0]['function']['parameters']['properties']['products']['items']['properties']['product_id']['enum']
 assert enum==['101'] and result['selectedProducts'][0]['productId']==101
