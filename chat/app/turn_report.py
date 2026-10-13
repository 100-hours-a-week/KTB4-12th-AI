"""Owner-visible report from allowlisted facts. No private profile or raw prompts."""
import math
from app.public_answer import public_products

def number(value):return value if type(value) in (int,float) and math.isfinite(value) and value>=0 else None

def report(cid,turn,context):
    usage=[{k:number(u.get(k)) for k in ('prompt_tokens','completion_tokens','cost')} for u in context.get('usage',[]) if isinstance(u,dict)]
    costs=[u['cost'] for u in usage]
    attempts=len(context['modelRequests']) if 'modelRequests' in context else len(usage)
    fixture=context.get('externalCalls')==0 or context.get('mode')=='mock'
    streaming=context.get('streaming',{})
    return {'schema':'chat-turn-report-v1','conversationId':cid,'turnId':turn['turnId'],
            'status':turn['status'],'mode':'protocol-fixture' if fixture else 'live',
            'revision':context.get('revision'),'model':context.get('model'),
            'message':turn['message'],'blocks':turn['blocks'],'lastEventId':turn['lastEventId'],
            'products':public_products(context.get('selectedProducts',[])),
            'catalogSnapshotId':context.get('snapshotId'),
            'metrics':{'elapsedMs':number(context.get('elapsedMs')),
                       'firstProviderAnswerMs':number(context.get('firstAnswerMs')),
                       'firstCommittedAnswerMs':number(streaming.get('firstCommittedDeltaMs')),
                       'firstCardsMs':number(context.get('firstCardsMs')),
                       'modelCalls':len(context['modelRequests']) if 'modelRequests' in context else len(usage),'modelCallsBasis':'reserved_transport_attempts' if 'modelRequests' in context else 'received_usage_records','modelAttempts':context.get('modelAttempts'),'agent':context.get('agent'),'toolBatches':context.get('toolBatches'),'providerTtftMs':number(context.get('providerTtftMs')),'firstMeaningfulMs':number(context.get('firstMeaningfulMs')),'requests':[{k:v for k,v in req.items() if k in ('index','model','reasoning','maxTokens','stream')} for req in context.get('modelRequests',[])],'searchBranches':number(context.get('searchBranches')) or 0,
                       'costUsd':0 if fixture else (sum(costs) if costs and len(costs)==attempts and all(v is not None for v in costs) else None),
                       'usage':usage,'stages':{k:number(v) for k,v in context.get('stages',{}).items() if k in ('planMs','selectMs','answerMs','model_1_ms','model_2_ms','prepare_ms','context_ms')}},
            'privacy':{'recipientPreferencesIncluded':False,'rawPromptsIncluded':False,'hiddenReasoningIncluded':False},
            'failureStage':context.get('agentFailureStage') if context.get('agentFailureStage') in ('prepare','context','model_1','model_2','validation') else None,
            'diagnosticCode':context.get('agentFailureCode'),'validationDiagnostics':context.get('validationDiagnostics',[]),'error':turn.get('error')}

def text_report(data):
    m=data['metrics']
    lines=['선잘알 턴 보고서',f"모드: {data['mode']} / 상태: {data['status']}",
           f"대화: {data['conversationId']}",f"턴: {data['turnId']}",f"revision: {data['revision']} / 모델: {data['model']}",
           '', '사용자: '+data['message'],'답변:']
    for block in data['blocks']:
        if block['type']=='text':lines.append(block['text'])
        else:
            for card in block['products']:lines.append(f"상품 {card['productId']}: {card['reason']}")
    lines.extend(['',f"전체 {m['elapsedMs']} ms / 첫 저장 답변 {m['firstCommittedAnswerMs']} ms / 첫 카드 {m['firstCardsMs']} ms",
                  f"usage 보고 호출 {m['modelCalls']} / 검색 분기 {m['searchBranches']} / 비용 USD {m['costUsd'] if m['costUsd'] is not None else '확인 불가'}",
                  '카탈로그: '+str(data['catalogSnapshotId']),
                  '비공개 취향·프롬프트·추론 원문은 이 보고서에 포함하지 않습니다.'])
    if data['mode']=='protocol-fixture':lines.append('고정 응답으로 HTTP/SSE 계약만 검증했습니다. 실제 추천 품질 검증 결과가 아닙니다.')
    return '\n'.join(lines)+'\n'
