"""Deterministic integration scenarios. No provider, search or profile clients.

Only the generator differs: admission, PostgreSQL events, recovery, cancellation
and HTTP/SSE transport use the same implementation as the real model runtime.
"""
import asyncio
import copy
import hashlib
from pathlib import Path

from app.errors import APIError
from app.runtime import Runtime

SCENARIOS = [
    {'key':'cards','label':'답변 + 카드','message':'/mock cards'},
    {'key':'text','label':'텍스트만','message':'/mock text'},
    {'key':'no_match','label':'검색 결과 없음','message':'/mock no_match'},
    {'key':'long','label':'긴 답변 · 문자 표시','message':'/mock long'},
    {'key':'slow','label':'느린 응답 · 취소','message':'/mock slow'},
    {'key':'delayed_tokens','label':'느린 토큰','message':'/mock delayed_tokens'},
    {'key':'reconnect','label':'부분 답변 · 재연결','message':'/mock reconnect'},
    {'key':'failed','label':'답변 중 실패','message':'/mock failed'},
    {'key':'timeout','label':'시간 초과 · 실패','message':'/mock timeout'},
]
PRODUCTS = [
    {'productId':900000001,'name':'[테스트] 상품 A','brand':'MOCK','price':10000},
    {'productId':900000002,'name':'[테스트] 상품 B','brand':'MOCK','price':20000},
    {'productId':900000003,'name':'[테스트] 상품 C','brand':'MOCK','price':30000},
]
ANSWERS = {
    'cards':'고정 응답 테스트입니다. 질문 내용과 무관하게 같은 답변과 테스트 상품 카드 3개를 보냅니다. 실제 추천이나 구매 정보가 아닙니다.',
    'text':'고정 텍스트 응답입니다. 한글과 이모지 🎁를 여러 이벤트로 나누어 보냅니다. 이 시나리오에는 상품 카드가 없습니다.',
    'no_match':'테스트 조건에 맞는 상품이 없습니다. 이 문구는 검색 결과 없음 화면을 확인하기 위한 고정 응답입니다.',
    'slow':'느린 응답 테스트입니다. 약 15초 동안 연결을 유지한 뒤 이 문장을 보냅니다. 기다리는 동안 생성 중지와 재연결을 확인할 수 있습니다.',
    'failed':'여기까지는 저장된 부분 답변입니다. 다음 단계에서 테스트용 실패가 발생합니다.',
    'delayed_tokens':'느린 토큰 테스트입니다. 한글과 이모지 🎁가 조금씩 도착합니다. 생성 중지 후에는 저장된 부분 답변만 남아야 합니다.',
    'reconnect':'첫 부분 답변이 저장됐습니다. 잠시 쉬는 동안 SSE 재연결을 눌러 보세요. 이어받은 문장은 중복 없이 완성돼야 합니다. 🎁',
    'timeout':'이 부분 답변 뒤에 테스트용 시간 초과가 발생합니다. 부분 답변과 FAILED 상태를 함께 확인하세요.',
    'long':'[MOCK] 긴 답변 표시 테스트\n'+''.join(f'{i:02d}. 한글·emoji 🎁·조합 문자 e\u0301·줄바꿈을 확인하세요. <script>는 실행되지 않는 텍스트입니다.\n' for i in range(1,41)),
}


def scenario_for(message):
    if not message.startswith('/mock'):return 'cards'
    scenario=message.removeprefix('/mock').strip()
    if scenario not in ANSWERS:raise APIError(400,'[MOCK] 지원하지 않는 시나리오입니다. 무료 시험 안내의 /mock 명령을 사용하세요.')
    return scenario


class NoExternalSearch:
    async def ready(self):return {'ready':True,'mode':'mock','externalCalls':False}
    async def close(self):pass


class SimulatedFailure(Exception):pass


class MockRuntime(Runtime):
    START_DELAY=.15
    SLOW_START_DELAY=15
    TOKEN_DELAY=.08
    DELAYED_TOKEN_DELAY=.6
    RECONNECT_DELAY=4
    TIMEOUT_DELAY=2
    def __init__(self,store,search,owner):
        self.init_state(store,search,owner)
        self.model='mock-v1'

    async def accept(self,cid,tid,message,recipient_context=None):
        scenario_for(message)  # Reject invalid controls before persisting a turn.
        return await super().accept(cid,tid,message,recipient_context=recipient_context)

    def trace_config(self):
        revision=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
        return {'promptVersion':revision,'settingsVersion':'mock-v1',
                'settings':{'mode':'mock','model':self.model,'profileEnabled':False,
                            'llmEnabled':False,'searchEnabled':False},'mode':'mock'}

    async def generate(self,cid,tid,receipt):
        conversation=await self.store.call('read',cid=cid)
        current=next(turn for turn in conversation['turns'] if turn['turnId']==tid)
        scenario=scenario_for(current['message'])
        supplied=await self.store.call('recipient_context',cid=cid,tid=tid)
        receipt.update(scenario=scenario,activeStage='mock',recipientProfile={'status':supplied['status'],'source':'BACKEND_REQUEST','data':supplied} if supplied is not None else {'status':'NOT_PROVIDED'},
                       snapshotId='mock-catalog-v1',selectedProducts=[],cards=[],externalCalls=0)
        await self.emit(cid,tid,'progress',{'message':'고정 응답 테스트 · '+scenario})
        await asyncio.sleep(self.SLOW_START_DELAY if scenario=='slow' else self.START_DELAY)
        answer=ANSWERS[scenario]
        width=96 if scenario=='long' else 7
        for start in range(0,len(answer),width):
            await self.emit(cid,tid,'answer.delta',{'delta':answer[start:start+width]})
            if scenario=='reconnect' and start==0:await asyncio.sleep(self.RECONNECT_DELAY)
            else:await asyncio.sleep(self.DELAYED_TOKEN_DELAY if scenario=='delayed_tokens' else self.TOKEN_DELAY)
        if scenario=='failed':
            raise SimulatedFailure('Intentional mock failure after partial answer')
        if scenario=='timeout':
            await asyncio.sleep(self.TIMEOUT_DELAY)
            raise TimeoutError('Intentional mock timeout')
        if scenario=='cards':
            receipt['selectedProducts']=copy.deepcopy(PRODUCTS)
            receipt['cards']=[{'productId':p['productId'],'reason':'[MOCK] '+p['name']+' 카드 표시 검증용입니다.'} for p in PRODUCTS]
            await self.emit(cid,tid,'cards',{'products':receipt['cards']})

    async def complete(self,*args,**kwargs):raise RuntimeError('External LLM calls are disabled in mock mode')
    async def stream_answer(self,*args,**kwargs):raise RuntimeError('External LLM calls are disabled in mock mode')
    async def recipient_profile(self,*args,**kwargs):return {'status':'DISABLED'}
    def failure_message(self,exc):
        if isinstance(exc,SimulatedFailure):return '[MOCK] 의도적으로 발생시킨 테스트 오류입니다.'
        if isinstance(exc,TimeoutError):return '[MOCK] 테스트용 시간 초과입니다. 실제 모델의 180초 제한을 기다리는 시험은 아닙니다.'
        return super().failure_message(exc)
    async def close(self):await self.stop_jobs()
