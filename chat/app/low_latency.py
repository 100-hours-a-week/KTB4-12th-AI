"""Production gift tool loop over durable HF turns; max two transport attempts."""
import copy,hashlib,json,time
from dataclasses import replace
from types import SimpleNamespace
from contextlib import aclosing
from pathlib import Path
from app.runtime import Runtime, _PROMPT_VERSION as _RUNTIME_PROMPT_VERSION
from app.agent import AgentRuntime
from app.errors import APIError
from app.gift_core.contracts import Filters,Profile
from app.gift_core.harness import Session
from app.gift_core.provider import sse_json,ProviderError
from app.gift_adapter import CatalogAdapter,ContextAdapter,PublicHarness,view,hf_facts
from app.diagnostics import operation,event
from app.public_answer import PUBLIC_ANSWER_VERSION,previous_public_products
from app.conversation_memory import history_messages,last_completed_turn
from app.model_trace import TRACE
from app.integrated_harness import IntegratedHarness, protocol_manifest

_ROOT=Path(__file__).parent
_PROMPT_VERSION=hashlib.sha256(_RUNTIME_PROMPT_VERSION.encode()+b''.join(p.read_bytes() for p in [_ROOT/'low_latency.py',_ROOT/'gift_adapter.py',_ROOT/'model_trace.py',*sorted((_ROOT/'gift_core').glob('*.py'))])).hexdigest()[:16]

class ProviderAdapter:
 mode='openrouter'
 def __init__(self,runtime,receipt):self.runtime=runtime;self.receipt=receipt
 async def stream(self,messages,tools,model):
  self.receipt['modelAttempts']=self.receipt.get('modelAttempts',0)+1
  if self.receipt['modelAttempts']>2:raise ProviderError('model_call_budget_exhausted')
  body=self.runtime.request_body(messages,1200,0.3)
  # The measured Luna low harness uses one bounded completion incl. reasoning.
  if body['model']=='openai/gpt-6-luna':body['max_tokens']=min(1200,self.runtime.model_config.max_output)
  # Omission is part of the PC patch contract. Do not let a provider's
  # implicit strict-schema normalization turn omitted fields into null/[] clears.
  wire_tools=copy.deepcopy(tools)
  for tool in wire_tools:
   if tool.get('type')=='function':tool['function'].setdefault('strict',False)
  body.update(stream=True,stream_options={'include_usage':True},tools=wire_tools,tool_choice='auto')
  provider=dict(body.get('provider',{}));provider.update(require_parameters=True,sort='latency',allow_fallbacks=False);body['provider']=provider
  self.receipt['activeStage']='responder'
  async with self.runtime.http.stream('POST','/chat/completions',json=body) as response:
   if response.status_code!=200:raise ProviderError('provider_http_'+str(response.status_code))
   # Claude can stream a narration block before a valid tool_use block.
   # Keep that narration out of durable answers, then consume usage to settle cost.
   hold_text=body['model']=='anthropic/claude-haiku-5.5' and bool(tools)
   buffered=[];text_size=0;has_tool=False;text_released=False;finish=None;has_text=False
   async for packet in sse_json(response):
    if not isinstance(packet,dict) or packet.get('error'):raise ProviderError('provider_packet_error')
    metadata={k:packet[k] for k in ('model','provider') if isinstance(packet.get(k),str)}
    if metadata and metadata not in self.receipt.setdefault('providerResponses',[]):self.receipt['providerResponses'].append(metadata)
    if isinstance(packet.get('usage'),dict):yield {'kind':'usage','value':packet['usage']}
    for choice in packet.get('choices',[]):
     if choice.get('error'):raise ProviderError('provider_choice_error')
     delta=choice.get('delta',{})
     if text_released and (delta.get('content') or delta.get('tool_calls')):raise ProviderError('provider_content_after_finish')
     if delta.get('content'):
      has_text=True
      if hold_text:
       value=delta['content']
       if not isinstance(value,str):raise ProviderError('invalid_answer')
       text_size+=len(value)
       if text_size>8000:raise ProviderError('invalid_answer')
       buffered.append(value)
      else:yield {'kind':'text','value':delta['content']}
     for call in delta.get('tool_calls',[]):
      has_tool=True
      yield {'kind':'tool','value':call}
     if choice.get('finish_reason') is not None:
      finish=choice['finish_reason']
      yield {'kind':'finish','value':finish}
    if hold_text and not has_tool and not text_released and any(c.get('finish_reason')=='stop' for c in packet.get('choices',[])):
     # stop determines an ordinary answer; accounting/[DONE] can arrive later.
     text_released=True
     for value in buffered:yield {'kind':'text','value':value}
     buffered=[]
   if hold_text and not has_tool and not text_released:
    for value in buffered:yield {'kind':'text','value':value}
   if (finish=='stop' and has_text or finish=='tool_calls' and has_tool) and hasattr(self.runtime,'provider_succeeded'):
    await self.runtime.provider_succeeded(response)
 async def close(self):pass

class LowLatencyRuntime(AgentRuntime):
 def harness_class(self):
  return IntegratedHarness
 agent_name='gift-tool-loop-v1'
 @property
 def model_config(self):
  return self.effective_config(super().model_config)
 def effective_config(self,config):
  if config and config.model=='openai/gpt-6-luna':return replace(config,reasoning={'enabled':True,'effort':'low','exclude':True})
  return config
 def trace_config(self):
  result=super().trace_config();
  result['promptVersion']=_PROMPT_VERSION
  result['settings']['agent']=self.agent_name;result['settingsVersion']=hashlib.sha256(json.dumps(result['settings'],sort_keys=True).encode()).hexdigest()[:16]
  return result
 async def generate(self,cid,tid,receipt):
  receipt.update(agent=self.agent_name,modelRequests=[],modelAttempts=0,publicAnswer={'version':PUBLIC_ANSWER_VERSION,'products':[]},activeStage='context')
  token=TRACE.set(receipt['modelRequests'])
  try:await self._generate(cid,tid,receipt)
  finally:TRACE.reset(token)
 async def _generate(self,cid,tid,receipt):
  conversation=await self.store.call('read',cid=cid)
  snapshot=await self.recipient_profile(cid,tid);receipt['recipientProfile']=snapshot
  # Missing/expired/revoked consent disables private enrichment, not ordinary
  # message-only BE chat. Other lookup failures must remain visible failures.
  if snapshot.get('source')=='AI_PROFILE_STORE' and snapshot.get('status')=='UNAVAILABLE' and snapshot.get('accessStatus')!='NOT_AUTHORIZED':
   raise APIError(503,'수신자 취향의 조회 권한 또는 상태를 확인할 수 없습니다.')
  previous=last_completed_turn(conversation['turns'],tid);old=await self.store.call('context',cid=cid,tid=previous['turnId']) if previous else {}
  prepared=self.admissions.get(tid,{}).get('prepared_context')
  # Metadata failure must not prevent a one-call ordinary conversation.
  meta,meta_error,_=await self.planning_metadata()
  if meta is None:meta={'snapshotId':'unavailable','categories':[]}
  catalog=CatalogAdapter(self.search,meta,receipt)
  data=snapshot.get('data') or {};raw_preferences='';disliked=[];disliked_tags=[]
  if snapshot.get('source') in {'BACKEND_REQUEST','AI_PROFILE_STORE'} and snapshot.get('status')=='AVAILABLE':
   raw_preferences=(data.get('giftPreference') or '') if snapshot.get('source')=='BACKEND_REQUEST' else ''
   disliked=[str(c['categoryId']) for c in data.get('dislikedCategories',[])]
   if snapshot.get('source')=='AI_PROFILE_STORE' and snapshot.get('analysisCurrent'):
    from profiling.vocab import AXES,query_text
    valid=lambda t:isinstance(t,dict) and t.get('axis') in AXES and isinstance(t.get('tag'),str) and bool(t['tag'].strip())
    likes=[q for t in data.get('preferredTags',[]) if valid(t) and (q:=query_text(t['axis'],t['tag']))]
    disliked_tags=[{'axis':t['axis'],'tag':t['tag']} for t in data.get('dislikedTags',[]) if valid(t)]
    # Only current analysis separates likes from dislikes. Never turn stored
    # raw prose into a positive query while that analysis is pending or failed.
    raw_preferences=' '.join(likes)
  elif snapshot.get('status')=='COMPLETED':
   raw_preferences=' '.join(data.get('preferredTags',[]));tags=set(data.get('dislikedTags',[]))
   disliked=[c['category_id'] for c in catalog.categories if c['category'] in tags or c['category_group'] in tags]
  disliked=list(dict.fromkeys(disliked))
  raw_preferences=raw_preferences[:500]
  binding=await self.store.call('binding',cid=cid)
  identity=hashlib.sha256(str(binding['recipientUserId']).encode()).hexdigest()
  prior_state=old.get('lowLatencyState',{}) if old.get('agent') in {'gift-tool-loop-v1',self.agent_name} else {}
  same=prior_state.get('recipientIdentity')==identity
  prior_filters=Filters.model_validate(prior_state.get('publicFilters',{})) if same else Filters()
  profile=Profile(recipient_id=str(data.get('recipientUserId','bound-recipient')),preferences=raw_preferences,disliked_category_ids=disliked,disliked_tags=disliked_tags)
  if prepared:
   if meta_error:raise meta_error
   if prepared['snapshotId']!=catalog.snapshot:raise APIError(409,'준비된 후보의 상품 snapshot이 변경됐습니다. 새 질문 ID로 후보를 다시 준비해 주세요.')
   profile=profile.model_copy(update={'candidate_ids':[str(x) for x in prepared['productIds']]})
  session=Session(cid,profile,prior_filters);session.public_exclusions=list(prior_filters.exclude_category_ids)
  session.filter_sources=copy.deepcopy(prior_state.get('filterSources',{})) if same else {}
  session.product_exclusion_sources=copy.deepcopy(prior_state.get('productExclusionSources',{})) if same else {}
  session.last_queries=copy.deepcopy(prior_state.get('lastQueries',[])) if same else []
  session.last_search=copy.deepcopy(prior_state.get('lastSearch',{})) if same and not prepared else {}
  durable_display=getattr(self.harness_class(),'durable_display_memory',False)
  if durable_display and same and not prepared and isinstance(prior_state.get('lastDisplayedProducts'),list):
   session.last_displayed_products=copy.deepcopy(prior_state['lastDisplayedProducts'])
  # Use actual completed public answers, never hidden reasoning or failed turns.
  user_history=history_messages(conversation['turns'],tid,include_assistant=True)
  session.history=user_history[:-1] if user_history and user_history[-1].get('content')==next(t['message'] for t in conversation['turns'] if t['turnId']==tid) else user_history
  if (same and prior_state.get('historyVersion') in getattr(self.harness_class(),'history_versions',(getattr(self.harness_class(),'variant',None),))
      and prior_state.get('historyThroughTurnId')==(previous or {}).get('turnId')
      and isinstance(prior_state.get('verifiedHistory'),list)):
   # Preserve actual successful tool turns across HTTP requests and reloads.
   # Failed/cancelled requests since the previous success remain marked as such.
   prior_index=next(i for i,t in enumerate(conversation['turns']) if t['turnId']==previous['turnId'])
   following=history_messages(conversation['turns'][prior_index+1:],tid,include_assistant=True)
   session.history=copy.deepcopy(prior_state['verifiedHistory'])+following[:-1]
  saved=[] if prepared else previous_public_products(old)
  if durable_display:
   # A names/gallery reply can differ from the last recommendation cards.
   # Restore those cards independently, and only for the same recipient.
   saved=[{'productId':p['product_id']} for p in prior_state.get('lastProducts',[])] if same and not prepared else []
  for fact in saved:
   # Stored selected public facts need authoritative typed category metadata too.
   row=next((p for p in prior_state.get('lastProducts',[]) if p['product_id']==str(fact['productId'])),None)
   if row:
    session.last_products.append(copy.deepcopy(row));catalog.products[row['product_id']]=copy.deepcopy(row)
  context=getattr(self.harness_class(),'context_adapter',ContextAdapter)(catalog)
  original_load=context.load
  async def recorded_load(owner,profile,filters,queries=(),expand=False,raw_pool=False):
   return await operation(receipt,'gift_context',{'queries':list(queries),'filters':filters.model_dump(),'expand_catalog':expand,'raw_pool':raw_pool},lambda:original_load(owner,profile,filters,queries,expand,raw_pool))
  context.load=recorded_load
  if meta_error:
   async def unavailable(*args,**kwargs):raise meta_error
   context.load=unavailable
  # Runtime owns the existing 180s turn deadline; HTTP retains its 75s idle limit.
  harness=self.harness_class()(catalog,context,ProviderAdapter(self,receipt),turn_timeout=None)
  receipt['harnessVariant']=getattr(harness,'variant','baseline')
  receipt['harnessProtocol']=protocol_manifest()
  message=next(t['message'] for t in conversation['turns'] if t['turnId']==tid)
  request=SimpleNamespace(message=message,intent_hint='gift' if prepared else 'auto',model=self.model_config.model)
  output={'status':None}
  async def emit(kind,value):
   if kind!='text':event(receipt,kind,value)
   now=lambda:round((time.monotonic()-receipt['startedMonotonic'])*1000)
   if kind=='text':
    if 'firstAnswerMs' not in receipt:receipt['firstAnswerMs']=now()
    await self.emit(cid,tid,'answer.delta',{'delta':value['delta']})
    receipt.setdefault('streaming',{}).setdefault('firstCommittedDeltaMs',now())
   elif kind in ('cards','catalog'):
    from app.card_contract import harness_cards
    payload=harness_cards(value['products'])
    facts=hf_facts(value['products']);receipt['selectedProducts']=facts
    receipt['publicAnswer']={'version':PUBLIC_ANSWER_VERSION,'products':facts}
    await self.emit(cid,tid,'cards',payload);receipt['firstCardsMs']=now()
   elif kind=='progress':await self.emit(cid,tid,'progress',value)
   elif kind=='error':
    import re
    code=value['code'];receipt['agentFailureCode']=code if isinstance(code,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}',code) else 'invalid_model_response'
    receipt['validationDiagnostics']=value.get('validation',[]);receipt['agentFailureStage']=value.get('stage')
    output['error']=receipt['agentFailureCode']
   elif kind=='metrics':
    receipt['usage']=value['usage'];receipt['stages']=value['stages'];receipt['agentFailureStage']=value.get('failure_stage');receipt['toolBatches']=value['tool_batches'];receipt['providerTtftMs']=value['ttft_ms'];receipt['firstMeaningfulMs']=value['first_meaningful_ms']
   elif kind=='done':output['status']=value['status']
  await harness.run(session,request,tid,emit)
  if output['status']=='cancelled':raise __import__('asyncio').CancelledError()
  if output['status']!='completed':raise ProviderError(output.get('error','gift_loop_failed'))
  public_filters=getattr(catalog,'public_filters',session.filters).model_dump()
  # A text-only turn may have resolved private dislikes in session.filters but
  # no catalog request. Never persist those as user-origin search exclusions.
  public_filters['exclude_category_ids']=list(getattr(session,'public_exclusions',public_filters['exclude_category_ids']))
  receipt['lowLatencyState']={'recipientIdentity':identity,'publicFilters':public_filters,'lastProducts':session.last_products,'catalogSnapshotId':catalog.snapshot,
      'filterSources':getattr(session,'filter_sources',{}),'lastQueries':getattr(session,'last_queries',[]),
      'productExclusionSources':getattr(session,'product_exclusion_sources',{}),
      'lastSearch':getattr(session,'last_search',{})}
  if getattr(harness,'durable_tool_history',False):
   receipt['lowLatencyState'].update(verifiedHistory=session.history,historyVersion=harness.variant,historyThroughTurnId=tid)
  if not receipt['publicAnswer']['products'] and session.last_products:receipt['publicAnswer']['products']=hf_facts(session.last_products)
  if hasattr(session,'last_displayed_products'):
   receipt['lowLatencyState']['lastDisplayedProducts']=copy.deepcopy(session.last_displayed_products)
  receipt['snapshotId']=catalog.snapshot if catalog.snapshot!='unavailable' else None
  receipt['publicAnswer']['version']=PUBLIC_ANSWER_VERSION
  receipt['modelRequestCount']=len(receipt['modelRequests'])
