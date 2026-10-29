"""Bounded administrator projection. No credentials, private profile or reasoning export."""
import copy, json, math, re, time
from datetime import datetime, timezone
from app.diagnostics import clean

DETAIL_TTL_SECONDS=86400
TEXT_LIMIT=4000
TRACE_LIMIT=24000

def count(v):return v if type(v) is int and 0<=v<=2**53-1 else None
def numeric(v):return v if type(v) in (int,float) and math.isfinite(v) and v>=0 else None
def identifier(v):return v if isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}',v) else None

def text(v,limit=TEXT_LIMIT):
    if not isinstance(v,str):return None
    value=clean(v)
    from app.llm_connection import ACTIVE_CONNECTION
    connection=ACTIVE_CONNECTION.get()
    if connection is not None and connection.client is not None:
        auth=connection.client.headers.get('Authorization','')
        secret=auth.removeprefix('Bearer ') if auth.startswith('Bearer ') else ''
        if secret:value=value.replace(secret,'[redacted]')
    value=re.sub(r'(?i)(?:bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|(?:api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;]+)','[redacted]',value)
    return value[:limit]

def usage(v):
    if not isinstance(v,dict):return None
    details=v.get('prompt_tokens_details');output=v.get('completion_tokens_details')
    details=details if isinstance(details,dict) else {};output=output if isinstance(output,dict) else {}
    return {'inputTokens':count(v.get('prompt_tokens')),'outputTokens':count(v.get('completion_tokens')),'cachedReadTokens':count(details.get('cached_tokens')),'cacheWriteTokens':count(details.get('cache_write_tokens')),'reasoningTokens':count(output.get('reasoning_tokens')),'costUsd':numeric(v.get('cost')),'basis':'provider-reported; cache and reasoning are subsets, not additive totals'}

def usage_totals(entries,expected=None):
    """Use complete provider receipts only; cache/reasoning are subsets."""
    complete=bool(entries) and (expected is None or len(entries)==expected) and all(
        isinstance(v,dict) and count(v.get('prompt_tokens')) is not None
        and count(v.get('completion_tokens')) is not None for v in entries)
    return {
        'inputTokens':sum(v['prompt_tokens'] for v in entries) if complete else None,
        'outputTokens':sum(v['completion_tokens'] for v in entries) if complete else None,
        'costUsd':sum(v['cost'] for v in entries) if complete and all(numeric(v.get('cost')) is not None for v in entries) else None,
        'usageState':'complete-observed-records' if complete else 'incomplete-or-not-recorded',
    }

def capture_request(body):
    """Actual send-time projection. Private fields are discarded before persistence."""
    messages=[]
    for m in body.get('messages',[])[:32]:
        role=m.get('role');value=m.get('content')
        entry={'role':role,'originalChars':len(value) if isinstance(value,str) else 0}
        if role=='system' and isinstance(value,str):
            before,sep,after=value.partition('STATE_JSON\n')
            if sep:
                try:
                    state=json.loads(after)
                    public={'filters','prepared_candidates','last_products','last_displayed_products','last_search','last_queries','persistent_product_exclusion_origins'}
                    state={k:(v if k in public else '[private or unrecognized state redacted before recording]') for k,v in state.items()}
                    value=before+sep+json.dumps(state,ensure_ascii=False)
                except ValueError:value=before+sep+'[state unavailable for safe projection]'
            else:value='[system input not recorded by this projection]'
        elif role=='tool':
            try:
                obj=json.loads(value);value=json.dumps({k:obj[k] for k in ('products','filters','retrieved_count','server_constraints') if k in obj},ensure_ascii=False)
            except (ValueError,TypeError):value='[tool data unavailable]'
        entry['content']=text(value)
        if m.get('tool_calls'):entry['toolNames']=[identifier(c.get('function',{}).get('name')) for c in m['tool_calls'][:4]]
        messages.append(entry)
    value={'capture':'actual-send-time-redacted','model':identifier(body.get('model')),'messages':messages,'tools':[identifier(t.get('function',{}).get('name')) for t in body.get('tools',[])[:8]],'privateData':'not retained; tool argument bodies and hidden reasoning excluded'}
    raw=json.dumps(value,ensure_ascii=False)
    if len(raw)>TRACE_LIMIT:return {'capture':'actual-send-time-redacted','model':value['model'],'messages':[{'role':m['role'],'originalChars':m['originalChars']} for m in messages],'truncated':True,'reason':'trace-preview-size-limit'}
    return value

def trace(cid,turn,context,*,details=False,created=None):
    calls=context.get('requestAudit') or [];diagnostics=context.get('diagnostics') or {};operations=diagnostics.get('operations') or []
    result={'schema':'request-trace-v1','conversationId':cid,'turnId':turn['turnId'],'sourceScope':context.get('sourceScope','unclassified-legacy'),'synthetic':True if context.get('sourceScope')=='synthetic-local' or context.get('mode')=='mock' or context.get('externalCalls')==0 else None,'environment':identifier(context.get('environment')),'revision':identifier(context.get('revision')),'sourceIdentity':{'releaseLabel':identifier(context.get('revision')),'promptVersion':identifier(context.get('promptVersion')),'integrationSourceSha256':identifier((context.get('harnessProtocol') or {}).get('integrationSourceSha256')),'basis':'recorded release label and loaded-code fingerprints; not inferred Git HEAD'},'connection':context.get('connection') if isinstance(context.get('connection'),dict) else None,'status':turn['status'],'executionMode':context.get('executionMode','mock' if context.get('mode')=='mock' or context.get('externalCalls')==0 else 'unknown'),'elapsedMs':numeric(context.get('elapsedMs')),'lastEventId':count(turn.get('lastEventId')),'persistence':context.get('tracePersistence'),'modelCalls':[],'timeline':[],'recording':{'calls':'recorded-redacted' if calls else 'not-recorded-legacy','detailsRequested':details,'detailAccessWindowSeconds':DETAIL_TTL_SECONDS,'storedRecordRetention':'existing Chat store policy; no new deletion job','maxPreviewCharsPerCall':TRACE_LIMIT,'rawPrivatePrompts':False,'hiddenReasoning':False}}
    for c in calls[:8]:
        item={k:c.get(k) for k in ('index','startedAt','status','elapsedMs','finishReason','truncated')};item['usage']=usage(c.get('usage'))
        if details:item.update(input=c.get('input'),output={'visibleText':text(c.get('visibleText')),'toolNames':c.get('toolNames',[]),'arguments':'not retained; final selected cards are shown separately'})
        result['modelCalls'].append(item);result['timeline'].append({'kind':'model','index':c.get('index'),'at':c.get('startedAt'),'offsetMs':numeric(c.get('offsetMs')),'status':c.get('status'),'elapsedMs':numeric(c.get('elapsedMs'))})
    if not calls:
        # Old usage is evidence, but never synthesize send-time input/output.
        result['legacyUsage']=[usage(v) for v in context.get('usage',[])[:8]]
    for operation in operations[:40]:
        row={'kind':'tool','name':identifier(operation.get('name')),'at':operation.get('startedAt'),'offsetMs':numeric(operation.get('turnOffsetMs')),'status':identifier(operation.get('status')),'elapsedMs':numeric(operation.get('elapsedMs'))}
        raw=operation.get('result')
        if isinstance(raw,dict) and isinstance(raw.get('products'),list):row['productIds']=[identifier(str(p.get('product_id',p.get('productId','')))) for p in raw['products'][:30]]
        result['timeline'].append(row)
    result['timeline'].sort(key=lambda x:(x.get('offsetMs') is None,x.get('offsetMs') or 0))
    result['timeline'].append({'kind':'stored-terminal-state','status':turn['status'],'lastEventId':count(turn.get('lastEventId')),'basis':'stored turn state; not independent proof every client received SSE'})
    entries=[c.get('usage') for c in calls] if calls else context.get('usage',[])
    expected=len(context['modelRequests']) if isinstance(context.get('modelRequests'),list) else None
    result['totals']={**usage_totals(entries,expected),'attemptsObserved':len(calls) if calls else None}
    if details:
        expired=False
        try:expired=(datetime.now(timezone.utc)-datetime.fromisoformat(created.replace('Z','+00:00'))).total_seconds()>DETAIL_TTL_SECONDS if created else True
        except (ValueError,TypeError):expired=True
        if expired:
            result['recording']['detailState']='expired-or-creation-time-unavailable'
            for c in result['modelCalls']:c.pop('input',None);c.pop('output',None)
        else:
            result['recording']['detailState']='bounded-redacted'
            result['publicInput']=text(turn.get('message'))
            result['publicOutput']=[{'type':b.get('type'),'text':text(b.get('text')),'products':[{'productId':p.get('productId'),'reason':text(p.get('reason'),200)} for p in b.get('products',[])[:3]]} for b in turn.get('blocks',[])[:20]]
    return clean(result)

def conversation_projection(data):
    result=copy.deepcopy(data)
    for turn in result.get('turns',[]):
        ctx=turn.get('context') or {};state=turn['state']
        turn['trace']=trace(result['conversationId'],state,ctx,created=turn.get('created'))
        turn['trace']['sseEvents']=turn.get('eventSummary',{'events':[],'basis':'not-recorded-in-this-projection','truncated':None})
        turn['context']={k:ctx[k] for k in ('model','elapsedMs','tracePersistence') if k in ctx}
        turn['state']={**state,'message':'[요청 원문은 상세 열람에서 확인]','blocks':[]}
        for review in turn.get('reviews',[]):review['note']='[검토 메모 원문 숨김]'
        for f in turn.get('feedback',[]):f['payload']={'verdict':f.get('payload',{}).get('verdict'),'note':'[상세 열람에서 확인]'}
    return result

def list_summary(raw):
    if not isinstance(raw,dict):return None
    entries=raw.get('usage') if isinstance(raw.get('usage'),list) else []
    expected=count(raw.get('attemptCount'))
    return {'turnId':identifier(raw.get('turnId')),'status':identifier(raw.get('status')),'environment':identifier(raw.get('environment')),'sourceScope':identifier(raw.get('sourceScope')) or 'unclassified-legacy','revision':identifier(raw.get('revision')),'promptVersion':identifier(raw.get('promptVersion')),'model':identifier(raw.get('model')),'elapsedMs':numeric(raw.get('elapsedMs')),'sourceVersion':count(raw.get('sourceVersion')),**usage_totals(entries,expected if expected is not None else 0)}
