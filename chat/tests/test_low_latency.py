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
def context(scope='current',budget=None):
 return {'queries':['책받침'],'changes':{'max_price':budget} if budget is not None else {}}
def show(final=True):return {'product_ids':['101']}
async def run(packets,prepared=None,private=False,old=None,message="합성 질문"):
 store=Store();search=Search();sent=[];cid,tid=str(uuid.uuid4()),str(uuid.uuid4())
 await store.call('conversation',cid=cid,sender=7,recipient=42,protocol_fixture=False,demo_profile=None)
 if old:
  oid=str(uuid.uuid4());store.states[oid]={'turnId':oid,'status':'COMPLETED','message':'이전 합성 요청','blocks':[],'lastEventId':1,'_cid':cid};store.traces[oid]=old
 if private:store.supplied={'recipientUserId':42,'status':'AVAILABLE','giftPreference':'PRIVATE_CANARY_BOOK_SUPPORT','dislikedCategories':[{'categoryId':20,'categoryName':'식품'}]}
 await store.call('start',cid=cid,tid=tid,message=message)
 def reply(req):
  sent.append(json.loads(req.content));packet=packets.pop(0)
  return httpx.Response(200,text='data: '+json.dumps(packet,ensure_ascii=False)+'\n\ndata: [DONE]\n\n')
 async with httpx.AsyncClient(base_url='https://synthetic',transport=httpx.MockTransport(reply)) as http:
  runtime=object.__new__(LowLatencyRuntime);runtime.init_state(store,search,'owner');runtime.model='openai/gpt-6-luna';runtime.catalog=ModelCatalog(http);runtime.http=BudgetHTTP(http,store)
  runtime.admissions[tid]={'prepared_context':prepared};receipt={'startedMonotonic':time.monotonic(),'usage':[]}
  await runtime.generate(cid,tid,receipt)
 return receipt,sent,store,search,cid,tid

# These are scripted decisions, not evidence that a real model interprets quantity correctly.
async def test_ordinary_chat_keeps_one_request_and_no_search():
 receipt,sent,store,search,*_=await run([text('안녕하세요.')])
 assert len(sent)==1 and not search.queries
 assert len([v for k,v in store.budget if k=='budget_reserve'])==1

def test_integrated_provenance_hashes_the_active_adapter():
 from app.integrated_harness import protocol_manifest
 from pathlib import Path
 record=protocol_manifest()
 assert record['version']=='stable-v4.12+ai-be-v1'
 assert {'integrated_harness.py','low_latency.py','gift_conversation.py'} <= record['sourceHashes'].keys()
 search=Path(__file__).resolve().parents[2]/'product-search/search'
 assert {name for name in ('service.py','models.py','common_api.py') if (search/name).is_file()} == record['retrievalSourceHashes'].keys()
 if not record['retrievalSourceHashes']:
  assert record['retrievalSourceLocation']=='remote-search; bind by receipt snapshotId'

@pytest.mark.parametrize('prepared_mode',[False,True])
async def test_native_decision_then_selection_preserves_transport_and_budget(prepared_mode):
 prepared={'productIds':[101],'snapshotId':'synthetic-v1'} if prepared_mode else None
 receipt,sent,store,search,cid,tid=await run([tool('gift_context',context(budget=20000)),tool('show_gifts',show())],prepared)
 assert len(sent)==len(receipt['modelRequests'])==2
 assert all(b['reasoning']=={'enabled':True,'effort':'low','exclude':True} and b['max_tokens']==1200 for b in sent)
 assert all(b['provider']['allow_fallbacks'] is False for b in sent)
 assert all(t['function']['strict'] is False for b in sent for t in b['tools'])
 fields=sent[0]['tools'][0]['function']['parameters']['properties']['changes']
 assert not fields.get('required')  # Omission must stay different from null/[] clearing.
 assert receipt['selectedProducts'][0]['productId']==101
 assert receipt['lowLatencyState']['publicFilters']['max_price']==20000
 assert len([v for k,v in store.budget if k=='budget_reserve'])==2
 if prepared_mode:assert search.queries[0]['filters']['includeProductIds']==[101]

async def test_private_profile_and_exclusions_stay_outside_model_and_report():
 receipt,sent,store,search,cid,tid=await run([tool('gift_context',context()),tool('show_gifts',show())],private=True)
 assert 'PRIVATE_CANARY' not in json.dumps(sent)
 assert receipt['lowLatencyState']['publicFilters']['exclude_category_ids']==[]
 assert all(20 in query['filters']['excludeCategoryIds'] for query in search.queries)
 assert 'PRIVATE_CANARY' not in json.dumps(report(cid,store.public(store.states[tid]),receipt))

@pytest.mark.parametrize('count',[1,2,3])
@pytest.mark.parametrize('message',['상품 하나만','2만원 이하 3명에게 줄 상품','이번에는 두 개','가능한 것만'])
async def test_model_selected_count_and_order_are_preserved_not_inferred(monkeypatch,count,message):
 products=[{**PRODUCT,'productId':101+i,'name':'합성 상품 '+str(i)} for i in range(3)]
 async def hydrate(self,ids,snapshot_id):return {'snapshotId':snapshot_id,'products':[p for p in products if p['productId'] in ids]}
 async def search(self,body,snapshot_id):return {'snapshotId':snapshot_id,'hits':products}
 monkeypatch.setattr(Search,'get_products',hydrate);monkeypatch.setattr(Search,'search',search)
 ids=[str(p['productId']) for p in reversed(products[:count])]
 receipt,sent,*_=await run([tool('gift_context',context()),tool('show_gifts',{'product_ids':ids})],message=message)
 assert [str(p['productId']) for p in receipt['selectedProducts']]==ids
 # Even the intentionally inconsistent 'one' + three-ID decision is preserved.
 # Natural-language interpretation is a separate, unmeasured model-quality concern.
 assert any(m.get('content')==message for m in sent[0]['messages'])

async def test_candidate_shortage_is_not_padded():
 receipt,*_=await run([tool('gift_context',context()),tool('show_gifts',show())],message='세 개 추천해줘')
 assert len(receipt['selectedProducts'])==1

async def test_search_can_end_in_explanation_without_forced_cards():
 receipt,sent,*_=await run([tool('gift_context',context()),text('연결 규격을 확인해 주세요.')])
 assert not receipt.get('selectedProducts') and len(sent)==2

async def test_invalid_id_cannot_become_public_card():
 with pytest.raises(Exception):
  await run([tool('gift_context',context()),tool('show_gifts',{'product_ids':['999']}),tool('show_gifts',{'product_ids':['999']})])

async def test_max_three_is_schema_validation_not_truncation():
 from app.integrated_harness import IntegratedHarness,PublicListing
 from pydantic import ValidationError
 with pytest.raises(ValidationError):IntegratedHarness.selection_model.model_validate({'product_ids':['1','2','3','4']})
 with pytest.raises(ValidationError):PublicListing.model_validate({'product_ids':['1','2','3','4']})

async def test_successive_request_retains_display_memory_and_quantity_change(monkeypatch):
 first,*_=await run([tool('gift_context',context()),tool('show_gifts',show())],message='하나만')
 second,sent,*_=await run([text('앞서 표시한 상품을 기억하고 있어요.')],old=first,message='방금 상품 뭐였지?')
 assert [p['product_id'] for p in second['lowLatencyState']['lastDisplayedProducts']]==['101']
 assert len(sent)==1
 third,sent,*_=await run([tool('gift_context',context(budget=30000)),tool('show_gifts',show())],old=second,message='이번엔 두 개, 없으면 하나만')
 assert len(third['selectedProducts'])==1 and third['lowLatencyState']['publicFilters']['max_price']==30000

async def test_snapshot_mismatch_fails_before_model_reservation():
 with pytest.raises(Exception):await run([],{'productIds':[101],'snapshotId':'stale'})

async def test_new_prepared_pool_does_not_restore_old_display():
 prior,*_=await run([tool('gift_context',context()),tool('show_gifts',show())])
 prior['lowLatencyState']['lastProducts'][0]['product_id']='201'
 prior['lowLatencyState']['lastDisplayedProducts'][0]['product_id']='201'
 result,sent,*_=await run([tool('gift_context',context()),tool('show_gifts',show())],{'productIds':[101],'snapshotId':'synthetic-v1'},old=prior)
 assert result['selectedProducts'][0]['productId']==101
 assert result['lowLatencyState']['lastDisplayedProducts'][0]['product_id']=='101'


def test_integrated_source_identity_covers_contract_limits_and_profiler_policy():
 from app.integrated_harness import protocol_manifest
 import hashlib,json
 from pathlib import Path
 record=protocol_manifest()
 assert {'models.py','limits.py'} <= record['sourceHashes'].keys()
 identity=record['integrationSourceIdentity']
 assert identity['version']=='integration-source/1'
 paths=set(identity['other'])|set(identity['unavailable'])
 assert len(paths)==13 and not set(identity['other'])&set(identity['unavailable'])
 assert {'profiling/src/profiling/selection.py','profiling/src/profiling/prompts/tag3disliked_system.md',
         'integration/profiler_rebind.py'} <= paths
 checkout=Path(__file__).resolve().parents[2]
 for name in paths:
  if (checkout/name).is_file():
   assert identity['other'][name]==hashlib.sha256((checkout/name).read_bytes()).hexdigest()
  else:
   assert name in identity['unavailable']
 assert record['integrationSourceSha256']==hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 assert protocol_manifest()['integrationSourceSha256']==record['integrationSourceSha256']
