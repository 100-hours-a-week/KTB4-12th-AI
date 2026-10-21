from test_hf_integration import SyntheticPrices
import asyncio,copy,json,time,uuid
import httpx,pytest
from app.low_latency import LowLatencyRuntime
from app.model_config import ModelCatalog
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
async def run(packets,prepared=None,private=False,old=None,message="합성 질문",stored=None):
 store=Store();search=Search();sent=[];cid,tid=str(uuid.uuid4()),str(uuid.uuid4())
 await store.call('conversation',cid=cid,sender=7,recipient=42,protocol_fixture=False,demo_profile=None)
 if old:
  oid=str(uuid.uuid4());store.states[oid]={'turnId':oid,'status':'COMPLETED','message':'이전 합성 요청','blocks':[],'lastEventId':1,'_cid':cid};store.traces[oid]=old
 if private:store.supplied={'recipientUserId':42,'status':'AVAILABLE','giftPreference':'PRIVATE_CANARY_BOOK_SUPPORT','dislikedCategories':[{'categoryId':20,'categoryName':'식품'}]}
 await store.call('start',cid=cid,tid=tid,message=message)
 if stored:store.traces[tid]={'recipientProfile':stored}
 def reply(req):
  sent.append(json.loads(req.content));packet=packets.pop(0)
  wire=packet if isinstance(packet,list) else [packet]
  return httpx.Response(200,text=''.join('data: '+json.dumps(part,ensure_ascii=False)+'\n\n' for part in wire)+'data: [DONE]\n\n')
 async with httpx.AsyncClient(base_url='https://openrouter.ai/api/v1',transport=httpx.MockTransport(reply),event_hooks={"request":[__import__("app.main",fromlist=["provider_key_request"]).provider_key_request]}) as http:
  runtime=object.__new__(LowLatencyRuntime);runtime.init_state(store,search,'owner');runtime.model='openai/gpt-6-luna';runtime.catalog=ModelCatalog(http);runtime.http=http
  runtime.admissions[tid]={'prepared_context':prepared};receipt={'startedMonotonic':time.monotonic(),'usage':[]}
  await runtime.generate(cid,tid,receipt)
 return receipt,sent,store,search,cid,tid

# These are scripted decisions, not evidence that a real model interprets quantity correctly.
async def test_ordinary_chat_keeps_one_request_and_no_search():
 receipt,sent,store,search,*_=await run([text('안녕하세요.')])
 assert len(sent)==1 and not search.queries
 assert not store.budget

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
 assert not store.budget
 if prepared_mode:assert search.queries[0]['filters']['includeProductIds']==[101]

async def test_private_profile_and_exclusions_stay_outside_model_and_report():
 receipt,sent,store,search,cid,tid=await run([tool('gift_context',context()),tool('show_gifts',show())],private=True)
 assert 'PRIVATE_CANARY' not in json.dumps(sent)
 assert receipt['lowLatencyState']['publicFilters']['exclude_category_ids']==[]
 assert all(20 in query['filters']['excludeCategoryIds'] for query in search.queries)
 assert 'PRIVATE_CANARY' not in json.dumps(report(cid,store.public(store.states[tid]),receipt))

@pytest.mark.parametrize('private',[False,True])
async def test_two_public_queries_keep_both_branches_with_private_retrieval(monkeypatch,private):
 # Exercise the active harness, including bounded merge and final hydration.
 queries=['책받침','파우치'];private_query='PRIVATE_CANARY_BOOK_SUPPORT'
 batches={q:[{**PRODUCT,'productId':base+i,'name':'합성 후보 '+str(base+i)} for i in range(15)]
          for q,base in [(private_query,1001),(queries[0],2001),(queries[1],3001)]}
 async def search(self,body,snapshot_id):
  self.queries.append(copy.deepcopy(body))
  return {'snapshotId':snapshot_id,'hits':batches[body['query']]}
 async def hydrate(self,ids,snapshot_id):
  return {'snapshotId':snapshot_id,'products':[p for rows in batches.values() for p in rows if p['productId'] in ids]}
 monkeypatch.setattr(Search,'search',search);monkeypatch.setattr(Search,'get_products',hydrate)
 receipt,sent,store,search,cid,tid=await run([
  tool('gift_context',{'queries':queries,'changes':{'max_price':20000}}),
  tool('show_gifts',{'product_ids':['3001']})],private=private)
 assert [q['query'] for q in search.queries]==([private_query] if private else [])+queries
 assert receipt['searchBranches']==(3 if private else 2)
 observed=[p['productId'] for p in receipt['observedProducts']]
 assert len(observed)==len(set(observed))==30
 assert {2001,3001} <= set(observed)
 assert receipt['selectedProducts'][0]['productId']==3001
 assert receipt['lowLatencyState']['lastSearch']['queries']==queries
 assert all(q['filters']['maxPrice']==20000 for q in search.queries)
 if private:assert all(20 in q['filters']['excludeCategoryIds'] for q in search.queries)
 assert private_query not in json.dumps(sent)
 assert private_query not in json.dumps(report(cid,store.public(store.states[tid]),receipt))

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


@pytest.mark.parametrize('axis,term,name,brand',[('카테고리','향수','합성 공간 향수','합성'),('카테고리','블록','합성 블록 장식품','합성'),('브랜드','ACME','합성 책받침','ACME')])
async def test_current_stored_dislikes_filter_across_categories_without_model_disclosure(monkeypatch,axis,term,name,brand):
 bad={**PRODUCT,'productId':102,'name':name,'brand':brand}
 async def search(self,body,snapshot_id):
  self.queries.append(copy.deepcopy(body))
  return {'snapshotId':snapshot_id,'hits':[bad,PRODUCT]}
 monkeypatch.setattr(Search,'search',search)
 snapshot={'source':'AI_PROFILE_STORE','status':'AVAILABLE','analysisCurrent':True,
  'data':{'recipientUserId':42,'sourceVersion':1,'giftPreference':'PRIVATE_RAW_DO_NOT_DISCLOSE',
   'dislikedCategories':[],'preferredTags':[{'axis':'카테고리','tag':'독서'}],'dislikedTags':[{'axis':axis,'tag':term}]}}
 receipt,sent,store,search,*_=await run([tool('gift_context',context()),tool('show_gifts',show())],stored=snapshot)
 assert [p['productId'] for p in receipt['observedProducts']]==[101]
 assert [p['productId'] for p in receipt['selectedProducts']]==[101]
 assert search.queries[0]['query']=='독서'
 assert 'PRIVATE_RAW_DO_NOT_DISCLOSE' not in json.dumps(sent)
 assert term not in json.dumps(sent,ensure_ascii=False)


async def test_unauthorized_stored_profile_allows_generic_message_only_chat():
 snapshot={'source':'AI_PROFILE_STORE','status':'UNAVAILABLE','accessStatus':'NOT_AUTHORIZED'}
 receipt,sent,store,search,*_=await run([text('안녕하세요.')],stored=snapshot)
 assert receipt['recipientProfile']==snapshot
 assert len(sent)==1 and not search.queries


@pytest.mark.parametrize('access_status',[None,'UNAVAILABLE','INVALID'])
async def test_other_unavailable_stored_profiles_still_stop_before_model_or_search(access_status):
 from app.errors import APIError
 packets=[text('must not be sent')]
 with pytest.raises(APIError,match='조회 권한'):
  await run(packets,stored={'source':'AI_PROFILE_STORE','status':'UNAVAILABLE','accessStatus':access_status})
 assert len(packets)==1


@pytest.mark.parametrize('analysis_status',['PENDING','FAILED','STALE'])
async def test_incomplete_analysis_never_uses_raw_dislikes_as_positive_query(analysis_status):
 raw='PRIVATE_RAW 향수는 싫습니다. 책받침을 좋아합니다.'
 snapshot={'source':'AI_PROFILE_STORE','status':'AVAILABLE','analysisCurrent':False,'analysisStatus':analysis_status,
  'data':{'recipientUserId':42,'sourceVersion':2,'giftPreference':raw,
   'dislikedCategories':[{'categoryId':20,'categoryName':'식품'}],
   'preferredTags':[{'axis':'카테고리','tag':'향수'}],'dislikedTags':[{'axis':'카테고리','tag':'독서'}]}}
 receipt,sent,store,search,*_=await run([tool('gift_context',context()),tool('show_gifts',show())],stored=snapshot)
 assert [q['query'] for q in search.queries]==['책받침']
 assert all(20 in q['filters']['excludeCategoryIds'] for q in search.queries)
 assert [p['productId'] for p in receipt['selectedProducts']]==[101]
 assert raw not in json.dumps(search.queries,ensure_ascii=False)
 assert 'PRIVATE_RAW' not in json.dumps(sent)


def test_stored_dislikes_apply_only_to_bound_recipient():
 from app.gift_adapter import CatalogAdapter,view
 from app.gift_core.contracts import Profile
 catalog=CatalogAdapter(None,META,{})
 profile=Profile(disliked_tags=[{'axis':'브랜드','tag':'ACME'}])
 item=view({**PRODUCT,'brand':'ACME'})
 current=catalog.resolve_filters(Filters(),Filters(),profile)
 assert not catalog.allowed(item,current)
 other=catalog.resolve_filters(Filters(recipient_scope='other'),Filters(),profile)
 assert catalog.allowed(item,other)

@pytest.mark.parametrize('axis,tag,name,brand,kind,allowed',[
 ('카테고리','차','자동차 미니어처','합성','모형자동차',True),
 ('브랜드','브라운','브라운 가죽 카드지갑','합성','카드지갑',True),
 ('브랜드','레고','레고트 텀블러','레고트','텀블러',True),
 ('IP','브라운','브라운 면도기','브라운','면도기',True),
 ('브랜드','소니','합성 헤드폰','SONY','헤드폰',False),
 ('IP','카카오프렌즈','춘식이 쿠션','합성','쿠션',False),
 ('카테고리','커피','아메리카노','합성','카페세트',False),
])
def test_stored_dislikes_preserve_axes_and_profiler_aliases(axis,tag,name,brand,kind,allowed):
 from app.gift_adapter import CatalogAdapter,view
 from app.gift_core.contracts import Profile
 catalog=CatalogAdapter(None,META,{})
 filters=catalog.resolve_filters(Filters(),Filters(),Profile(disliked_tags=[{'axis':axis,'tag':tag}]))
 item=view({**PRODUCT,'name':name,'brand':brand,'productKind':kind})
 assert catalog.allowed(item,filters) is allowed

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

@pytest.mark.parametrize('message',[None,'',' \n '])
@pytest.mark.parametrize('operation',['show_gifts','list_gifts'])
async def test_optional_card_introduction_never_persists_empty_text(message,operation):
 payload={'product_ids':['101']}
 if operation=='list_gifts':payload['format']='brief'
 if message is not None:payload['message']=message
 receipt,sent,store,search,cid,tid=await run([tool('gift_context',context()),tool(operation,payload)])
 assert [e['event'] for e in store.events[tid]]==['progress','cards']
 assert store.states[tid]['blocks']==[{'type':'cards','products':[{'productId':101,'reason':'상품 정보: 책을 올려 두는 합성 테스트 상품'}]}]
 assert len(sent)==2 and not store.budget
 assert receipt['selectedProducts'][0]['productId']==101

async def test_meaningful_card_introduction_remains_in_stream_and_saved_state():
 message='책을 올려 두기 좋은 상품이에요.'
 receipt,sent,store,search,cid,tid=await run([tool('gift_context',context()),tool('show_gifts',{'product_ids':['101'],'message':message})])
 assert [e['data']['delta'] for e in store.events[tid] if e['event']=='answer.delta']==[message]
 assert store.states[tid]['blocks'][0]=={'type':'text','text':message}

async def test_ordinary_stream_preserves_spaces_and_newlines_between_chunks():
 chunks=['가',' ','나','\n','다']
 packets=[{'choices':[{'delta':{'content':chunk},'finish_reason':'stop' if i==len(chunks)-1 else None}]} for i,chunk in enumerate(chunks)]
 packets[-1]['usage']={'prompt_tokens':10,'completion_tokens':10,'cost':.001}
 receipt,sent,store,search,cid,tid=await run([packets])
 assert [e['data']['delta'] for e in store.events[tid] if e['event']=='answer.delta']==chunks
 assert store.states[tid]['blocks']==[{'type':'text','text':''.join(chunks)}]
 assert len(sent)==1 and not search.queries
