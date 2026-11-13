"""Bounded administrator projection. Preferences and tool evidence; credentials and hidden reasoning excluded."""
import copy, json, math, re, time
from datetime import datetime, timezone
from typing import cast
from app.diagnostics import clean, bounded, FIELD_LIMIT

DETAIL_TTL_SECONDS=86400
TEXT_LIMIT=4000
TRACE_LIMIT=24000

def count(v):return v if type(v) is int and 0<=v<=2**53-1 else None
def numeric(v):return v if type(v) in (int,float) and math.isfinite(v) and v>=0 else None
def identifier(v):return v if isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}',v) else None

def text(v,limit=TEXT_LIMIT):
    if not isinstance(v,str):return None
    value=cast(str,clean(v))
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
    """Record actual model input for authenticated diagnostics, excluding credentials."""
    value={'capture':'actual-send-time-credential-filtered',
           'model':identifier(body.get('model')),
           'messages':[{k:v for k,v in m.items() if k in {'role','content','tool_calls','tool_call_id','name'}} for m in body.get('messages',[])], 'tools':body.get('tools',[]),
           'privateData':'preferences and tool arguments retained; credentials excluded'}
    return bounded(value)


def call_diagnostics(call):
    result={k:numeric(call.get(k)) for k in ('httpStatus','headersMs','firstPacketMs','firstTextMs','firstToolMs')}
    if result['httpStatus'] is not None and not 100<=result['httpStatus']<=599:result['httpStatus']=None
    result['errorType']=identifier(call.get('errorType'))
    limits=call.get('timeouts') if isinstance(call.get('timeouts'),dict) else {}
    result['timeouts']={k:numeric(limits.get(k)) for k in ('connectSeconds','readSeconds','writeSeconds','poolSeconds')}
    return result

def profile_diagnostics(value):
    """State/version evidence only; preference prose stays in explicit detail."""
    value=value if isinstance(value,dict) else {}
    data=value.get('data') if isinstance(value.get('data'),dict) else {}
    def state(key,allowed):return value.get(key) if value.get(key) in allowed else None
    return {'status':state('status',('AVAILABLE','UNAVAILABLE','NONE','NOT_PROVIDED','TEAM_SESSION','COMPLETED','PENDING','PROCESSING','FAILED')),
            'accessStatus':state('accessStatus',('NOT_AUTHORIZED','UNAVAILABLE','INVALID','AUTHORIZED')),
            'analysisStatus':state('analysisStatus',('PENDING','PROCESSING','COMPLETED','FAILED','UNAVAILABLE')),
            'analysisCurrent':value.get('analysisCurrent') if type(value.get('analysisCurrent')) is bool else None,
            'inputSourceVersion':count(value.get('inputSourceVersion')),
            'analysisSourceVersion':count(value.get('analysisSourceVersion')),
            'dataSourceVersion':count(data.get('sourceVersion'))}

def frame_diagnostics(value):
    result=[]
    for frame in value[:8] if isinstance(value,list) else []:
        if not isinstance(frame,dict):continue
        module=identifier(frame.get('module'));function=frame.get('function');line=count(frame.get('line'))
        if (module and module.startswith(('app.','integration.','profiling.','search.'))
                and isinstance(function,str) and re.fullmatch(r'[A-Za-z0-9_.<>]{1,100}',function)
                and line is not None and line>0):
            result.append({'module':module,'function':function,'line':line})
    return result

def failure_diagnostics(status,context):
    calls=context.get('requestAudit') or []
    last=calls[-1] if calls and isinstance(calls[-1],dict) else context.get('lastModelCall') or {}
    code=identifier(context.get('agentFailureCode')) or identifier(last.get('errorType')) or identifier(context.get('failure'))
    stage=identifier(context.get('agentFailureStage')) or identifier(context.get('failureStage')) or identifier(context.get('activeStage'))
    model_stage=stage in {'model_1','model_2','responder','plan','answer'}
    model_http_status=call_diagnostics(last)['httpStatus']
    http_status=call_diagnostics({'httpStatus':context.get('failureStatus')})['httpStatus'] or (model_http_status if model_stage else None)
    status_source=identifier(context.get('failureStatusSource')) if context.get('failureStatus') is not None else 'model_response' if model_stage and model_http_status is not None else None
    category='unrecorded'
    if status=='COMPLETED':category='completed'
    elif code in {'recipient_profile_unavailable','recipient_profile_stale'}:category=code
    elif status_source=='application' and http_status is not None:category='application_error'
    elif code=='turn_deadline_exceeded':category='turn_deadline'
    elif context.get('interrupted') or status=='CANCELLED':category='interrupted'
    elif code in ('ReadTimeout','ConnectTimeout','WriteTimeout','PoolTimeout'):
        category={'ReadTimeout':'provider_read_timeout','ConnectTimeout':'provider_connect_timeout','WriteTimeout':'provider_write_timeout','PoolTimeout':'provider_pool_timeout'}[code] if model_stage else 'dependency_timeout'
    elif http_status is not None and 400<=http_status<=599:category='provider_http_error' if model_stage else 'dependency_http_error'
    elif status=='FAILED':category='failed'
    elif status=='PROCESSING':category='in_progress'
    return {'category':category,'code':code,'exceptionType':identifier(context.get('failure')),
            'stage':stage,'profile':profile_diagnostics(context.get('recipientProfile')),
            'frames':frame_diagnostics(context.get('failureFrames')),
            'httpStatus':http_status,'httpStatusSource':status_source,'lastModelHttpStatus':model_http_status,'startedAt':context.get('startedAt'),'capturedAt':context.get('timingCapturedAt'),
            'turnTimeoutSeconds':numeric(context.get('turnTimeoutSeconds')),
            'firstAnswerMs':numeric(context.get('firstAnswerMs')),'firstCommittedDeltaMs':numeric(context.get('firstCommittedDeltaMs')),
            'checkpointWriteFailures':count(context.get('checkpointWriteFailures')),
            'delivery':'not-observed; stored AI events do not prove BE or client receipt'}

def trace(cid,turn,context,*,details=False,created=None,reviews=(),feedback=()):
    calls=context.get('requestAudit') or [];diagnostics=context.get('diagnostics') or {};operations=diagnostics.get('operations') or []
    result={'schema':'request-trace-v1','conversationId':cid,'turnId':turn['turnId'],'sourceScope':context.get('sourceScope','unclassified-legacy'),'synthetic':True if context.get('sourceScope')=='synthetic-local' or context.get('mode')=='mock' or context.get('externalCalls')==0 else None,'environment':identifier(context.get('environment')),'revision':identifier(context.get('revision')),'sourceIdentity':{'buildCommit':context.get('buildCommit') if isinstance(context.get('buildCommit'),str) and re.fullmatch(r'[0-9a-f]{40}',context['buildCommit']) else None,'releaseLabel':identifier(context.get('revision')),'promptVersion':identifier(context.get('promptVersion')),'integrationSourceSha256':identifier((context.get('harnessProtocol') or {}).get('integrationSourceSha256')),'basis':'recorded release label and loaded-code fingerprints; not inferred Git HEAD'},'connection':context.get('connection') if isinstance(context.get('connection'),dict) else None,'status':turn['status'],'executionMode':context.get('executionMode','mock' if context.get('mode')=='mock' or context.get('externalCalls')==0 else 'unknown'),'elapsedMs':numeric(context.get('elapsedMs')),'lastEventId':count(turn.get('lastEventId')),'persistence':context.get('tracePersistence'),'modelCalls':[],'timeline':[],'recording':{'calls':'recorded-credential-filtered' if calls else 'not-recorded-legacy','detailsRequested':details,'detailAccessWindowSeconds':DETAIL_TTL_SECONDS,'storedRecordRetention':'existing Chat store policy; no new deletion job','maxPreviewCharsPerCall':FIELD_LIMIT,'preferencesAndToolData':True,'rawPrivatePrompts':True,'hiddenReasoning':False}}
    result['diagnostic']=failure_diagnostics(turn['status'],context)
    # Empty audit data alone says nothing about whether a model was called.
    # The instrumented tool loop records both counters before any preparation.
    not_started=(context.get('agent')=='gift-tool-loop-v1'
                 and type(context.get('modelAttempts')) is int and context['modelAttempts']==0
                 and context.get('modelRequests')==[] and not calls
                 and not context.get('usage') and not diagnostics.get('modelCalls'))
    if not_started:result['recording']['calls']='not-started'
    for c in calls[:8]:
        item={k:c.get(k) for k in ('index','startedAt','status','elapsedMs','finishReason','truncated')};item['usage']=usage(c.get('usage'))
        item.update(call_diagnostics(c))
        if details:item.update(input=bounded(c.get('input')),output={'visibleText':text(c.get('visibleText'),FIELD_LIMIT),'toolNames':c.get('toolNames',[]),'toolCalls':bounded(c.get('toolCalls',[]))})
        result['modelCalls'].append(item);result['timeline'].append({'kind':'model','index':c.get('index'),'at':c.get('startedAt'),'offsetMs':numeric(c.get('offsetMs')),'status':c.get('status'),'elapsedMs':numeric(c.get('elapsedMs')),**call_diagnostics(c)})
    if not calls:
        # Old usage is evidence, but never synthesize send-time input/output.
        result['legacyUsage']=[usage(v) for v in context.get('usage',[])[:8]]
    for step in context.get('preparationSteps',[])[:32]:
        result['timeline'].append({'kind':'preparation','name':identifier(step.get('name')),
            'at':step.get('startedAt'),'offsetMs':numeric(step.get('turnOffsetMs')),
            'status':identifier(step.get('status')),'elapsedMs':numeric(step.get('elapsedMs')),
            'errorType':identifier(step.get('errorType'))})
    for operation in operations:
        row={'kind':'tool','name':identifier(operation.get('name')),'at':operation.get('startedAt'),'offsetMs':numeric(operation.get('turnOffsetMs')),'status':identifier(operation.get('status')),'elapsedMs':numeric(operation.get('elapsedMs')),'errorType':identifier(operation.get('errorType'))}
        raw=operation.get('result')
        if isinstance(raw,dict) and isinstance(raw.get('products'),list):row['productIds']=[identifier(str(p.get('product_id',p.get('productId','')))) for p in raw['products'][:30]]
        result['timeline'].append(row)
    result['timeline'].sort(key=lambda x:(x.get('offsetMs') is None,x.get('offsetMs') or 0))
    result['timeline'].append({'kind':'stored-terminal-state','status':turn['status'],'lastEventId':count(turn.get('lastEventId')),'basis':'stored turn state; not independent proof every client received SSE'})
    entries=[c.get('usage') for c in calls] if calls else context.get('usage',[])
    expected=len(context['modelRequests']) if isinstance(context.get('modelRequests'),list) else None
    result['totals']={**usage_totals(entries,expected),'attemptsObserved':len(calls) if calls else 0 if not_started else None}
    if details:
        expired=False
        try:expired=(datetime.now(timezone.utc)-datetime.fromisoformat(created.replace('Z','+00:00'))).total_seconds()>DETAIL_TTL_SECONDS if created else True
        except (ValueError,TypeError):expired=True
        if expired:
            result['recording']['detailState']='expired-or-creation-time-unavailable'
            for c in result['modelCalls']:c.pop('input',None);c.pop('output',None)
        else:
            result['recording']['detailState']='bounded-credential-filtered'
            result['recipientProfile']=bounded(context.get('recipientProfile'))
            result['toolOperations']=[bounded(o) for o in operations]
            result['recording']['operationsTruncated']=bool(diagnostics.get('truncated'))
            result['publicInput']=text(turn.get('message'))
            result['noteRecords']={'reviewsTotal':len(reviews),'feedbackTotal':len(feedback),'limitPerKind':40,'truncated':len(reviews)>40 or len(feedback)>40,'order':'latest retained records in stored order'}
            result['reviewNotes']=[{'note':text(r.get('note'))} for r in reviews[-40:]]
            result['feedbackNotes']=[{'verdict':text((f.get('payload') or {}).get('verdict'),100),'note':text((f.get('payload') or {}).get('note'))} for f in feedback[-40:]]
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
    return {'turnId':identifier(raw.get('turnId')),'created':raw.get('created'),'diagnostic':failure_diagnostics(raw.get('status'),raw),'status':identifier(raw.get('status')),'environment':identifier(raw.get('environment')),'backend':identifier(raw.get('backend')),'connectionRevision':count(raw.get('connectionRevision')),'sourceScope':identifier(raw.get('sourceScope')) or 'unclassified-legacy','revision':identifier(raw.get('revision')),'promptVersion':identifier(raw.get('promptVersion')),'model':identifier(raw.get('model')),'elapsedMs':numeric(raw.get('elapsedMs')),'sourceVersion':count(raw.get('sourceVersion')),**usage_totals(entries,expected if expected is not None else 0)}
