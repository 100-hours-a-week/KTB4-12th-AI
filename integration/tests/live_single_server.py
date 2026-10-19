"""Explicit opt-in paid scenario, fixed synthetic inputs, real Search/Profiler/Chat.

This file is not discovered by the free runner. Use --live only after verifying
the existing cumulative ledger and connection. No credential creation/import.
"""
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx
import psycopg
import pytest

from integration.shared_budget import ACCOUNT
from integration.tests.test_single_server import database, source

ROOT = Path(__file__).resolve().parents[2]
MODEL = 'openai/gpt-6-luna'
PREFERENCE = '차와 커피를 좋아해요. 머그컵도 좋아해요. 소니 제품과 향초는 싫어요.'
CASES = [
    {'message': '친구에게 줄 선물을 찾고 있어요. 3만원 이하로 두 개만 골라 주세요.', 'maxCount': 2, 'maxPrice': 30000},
    {'message': '커피는 빼고, 따뜻한 차를 마실 때 쓸 수 있는 것으로 한 개만 다시 골라 주세요.', 'maxCount': 1, 'maxPrice': 30000, 'expectedMaxFilter': 30000, 'excluded': [202, 203]},
    {'message': '예산을 2만원 이하로 낮추고 다른 것으로 바꿔 주세요.', 'maxCount': 1, 'maxPrice': 20000, 'excluded': [202, 203]},
    {'message': '예산은 100원 이하이고 소니 제품과 향초는 제외해 주세요. 조건에 맞는 선물 세 개를 찾아 주세요.', 'maxCount': 0, 'maxPrice': 100},
    {'message': '가격 제한은 없애고, 가장 비싼 머그컵 한 개만 보여 주세요.', 'maxCount': 1, 'maxPrice': 1000000000, 'expectedMaxFilter': None},
]


# Focused follow-up uses the same original five turns, plus explicit persistence
# and release controls. Sixteen Chat calls max; do not repeat unchanged tagging.
EXCLUSION_CASES = [{**case, 'expectedCount': count, 'expectedExcludedProductIds': []}
                   for case, count in zip(CASES, [2, 1, 1, 0, 1])]
EXCLUSION_CASES[-1]['expectedIds'] = [103]
EXCLUSION_CASES += [
    {'message': '보온 머그컵은 앞으로 계속 제외하고, 2만원 이하 컵 중 가장 비싼 것 하나만 보여 주세요.',
     'maxCount': 1, 'maxPrice': 20000, 'expectedCount': 1, 'expectedIds': [102], 'expectedExcludedProductIds': ['103']},
    {'message': '가격 상한만 없애고 컵 중 가장 비싼 것 하나만 보여 주세요.',
     'maxCount': 1, 'maxPrice': 1000000000, 'expectedCount': 1, 'expectedIds': [102], 'expectedMaxFilter': None, 'expectedExcludedProductIds': ['103']},
    {'message': '보온 머그컵 제외도 해제하고 컵 중 가장 비싼 것 하나만 보여 주세요.',
     'maxCount': 1, 'maxPrice': 1000000000, 'expectedCount': 1, 'expectedIds': [103], 'expectedExcludedProductIds': []},
]


def synthetic_catalog(source):
    root, export, categories, seal, embed, calls = source
    categories['data']['categories'] = [
        {'categoryId': 1, 'name': '생활', 'children': [{'categoryId': 11, 'name': '컵'}, {'categoryId': 41, 'name': '향초'}]},
        {'categoryId': 2, 'name': '식품', 'children': [{'categoryId': 21, 'name': '차'}, {'categoryId': 22, 'name': '커피'}, {'categoryId': 51, 'name': '과자'}]},
        {'categoryId': 3, 'name': '전자', 'children': [{'categoryId': 31, 'name': '이어폰'}]},
    ]
    rows = [
        (101, '흰색 도자기 머그컵', '합성생활', 11, '컵', 15000, '따뜻한 차와 커피용 도자기 머그컵 350ml'),
        (102, '내열 유리 찻잔', '합성생활', 11, '컵', 18000, '따뜻한 차를 담는 내열 유리 찻잔 300ml'),
        (103, '보온 머그컵', '합성생활', 11, '컵', 24000, '뚜껑이 있는 따뜻한 차 보온 머그컵'),
        (201, '녹차 티백 선물 세트', '합성차', 21, '차', 22000, '녹차 티백 20개 선물 포장'),
        (202, '디카페인 커피 원두', '합성커피', 22, '커피', 19000, '디카페인 커피 원두 200g'),
        (203, '드립 커피 선물 상자', '합성커피', 22, '커피', 35000, '드립백 커피 30개 선물 포장'),
        (301, '소니 무선 이어폰 합성 상품', '소니', 31, '이어폰', 45000, '검증 전용 합성 무선 이어폰'),
        (302, '앵커 무선 이어폰 합성 상품', '앵커', 31, '이어폰', 39000, '검증 전용 합성 무선 이어폰'),
        (401, '라벤더 향초', '합성향', 41, '향초', 17000, '라벤더 향의 실내 향초'),
        (501, '쿠키 선물 상자', '합성과자', 51, '과자', 12000, '버터 쿠키 선물 포장'),
    ]
    export['data']['products'] = [dict(productId=i, name=n, brand=b, categoryId=c, categoryName=cn,
        price=p, description=d, available=True, viewCount=i, updatedAt='2026-10-16T00:00:00Z')
        for i, n, b, c, cn, p, d in rows]
    seal()
    return source


@pytest.mark.parametrize('database', ['fresh'], indirect=True)
def test_actual_profiler_and_chat_models(database, source):
    assert os.getenv('SINGLE_SERVER_LIVE_APPROVED') == '1'
    from integration.tests.single_server_http import run
    options = {'cases': EXCLUSION_CASES, 'include_profiler': False} if os.getenv('SINGLE_SERVER_LIVE_CASE') == 'exclusion' else {}
    assert run(database, synthetic_catalog(source), live=True, live_options=options)['passed']


def run_models(database, export, contract, search_url, be, callbacks, token, output, start, wait, *, cases=CASES, include_profiler=True):
    # Only previously injected credentials are used. No key ever goes to disk.
    key, store_token = os.environ['OPENROUTER_API_KEY'], os.environ['STORE_TOKEN']
    evidence = {'synthetic': True, 'mock': False, 'modelFallback': False, 'requestedModel': MODEL,
                'catalog': export, 'contract': contract, 'cases': cases, 'profilerLiveIncluded': include_profiler, 'conversations': [],
                'realBEConnected': False, 'linuxImagesVerified': False,
                'localFence': {'maxAttempts': 16, 'maxReservedUsd': .25},
                'scope': 'fixed representative smoke; not a holdout or aggregate quality benchmark'}
    budget_url = None
    def budget(http):
        response = http.post(budget_url+'/rpc', headers={'Authorization': 'Bearer '+store_token},
                             json={'method': 'budget_summary', 'args': {'account': ACCOUNT}})
        response.raise_for_status()
        return response.json()['result']
    with httpx.Client(headers={'Authorization': 'Bearer '+token}, timeout=120, trust_env=False) as http:
        try:
            gateway, budget_url = start('budget', 'chat', 'integration.budget_gateway:app', {
                'PYTHONPATH': str(ROOT), 'STORE_TOKEN': store_token,
                'LIVE_VALIDATION_BUDGET_DB': str(output/'cost-fence.sqlite3'),
                'LIVE_VALIDATION_MAX_ATTEMPTS': '16', 'LIVE_VALIDATION_MAX_RESERVED_USD': '.25'})
            deadline = time.monotonic()+30
            while True:
                assert gateway.poll() is None
                try:
                    before = budget(http)
                    break
                except httpx.ConnectError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.1)
            evidence['budgetBefore'] = before
            assert before['limitUsd'] == 5 and not before['halted'] and before['remainingUsd'] >= .25
            body = {'recipientUserId': 777, 'sourceVersion': 1, 'giftPreference': PREFERENCE,
                    'dislikedCategories': [{'categoryId': 41, 'categoryName': '향초'}], 'reviews': []}
            if include_profiler:
                profile, profile_url = start('profiler-live', 'profiling', 'integration.tests.live_profiler_runtime:app', {
                    'PYTHONPATH': os.pathsep.join(map(str, [ROOT/'profiling/src', ROOT, ROOT/'product-search'])),
                    'PROFILING_DATABASE_URL': database['profiler'].replace('postgresql://', 'postgresql+psycopg://'),
                    'PROFILING_APP_DB_ROLE': 'ai_profiler_service', 'PROFILING_SERVICE_TOKEN': token,
                    'PROFILING_BACKEND_BASE_URL': be, 'PROFILING_TAGGER': 'openrouter',
                    'PROFILING_TAG_SPEC': 'tag3disliked', 'PROFILING_TAGGER_MODEL': MODEL,
                    'PROFILING_TAGGER_REQUIRED': 'true', 'PROFILING_EMBEDDING_REQUIRED': 'true',
                    'PROFILING_OPENROUTER_API_KEY': key, 'PROFILING_BUDGET_URL': budget_url, 'STORE_TOKEN': store_token,
                    'AI_BUDGET_MODE': 'guarded',
                    'PROFILING_CATALOG_SOURCE': 'search_http', 'PROFILING_SEARCH_BASE_URL': search_url,
                    'PROFILING_SEARCH_TOKEN': token, 'PROFILING_STRICT_STARTUP': 'true',
                    'PROFILING_CALLBACK_MAX_ATTEMPTS': '1', 'INTEGRATION_SYNTHETIC_ONLY': '1',
                    'SYNTHETIC_LLM_TRACE': str(output/'profiler-provider.jsonl')})
                wait(http, profile_url, profile)
                evidence['profileHealth'] = http.get(profile_url+'/health').json()
                body = {'recipientUserId': 777, 'sourceVersion': 1, 'giftPreference': PREFERENCE,
                        'dislikedCategories': [{'categoryId': 41, 'categoryName': '향초'}], 'reviews': []}
                evidence['profileInput'] = body
                assert http.post(profile_url+'/api/internal/v1/ai/profile/extract-and-pool', json=body).status_code == 202
                deadline = time.monotonic()+100
                while time.monotonic() < deadline:
                    with psycopg.connect(database['owner']) as db:
                        row = db.execute('SELECT to_jsonb(r) FROM ai_profile.profile_runs r WHERE recipient_user_id=777 AND source_version=1').fetchone()
                    if row and row[0]['status'] not in ('RUNNING', 'RESULT_READY'):
                        evidence['profileRun'] = row[0]
                        break
                    time.sleep(.15)
                else:
                    raise AssertionError('Real Profiler did not finish')
                assert evidence['profileRun']['status'] == 'DELIVERED'
                with psycopg.connect(database['owner']) as db:
                    evidence['profile'] = db.execute('SELECT to_jsonb(r) FROM ai_profile.recipient_profiles r WHERE recipient_user_id=777').fetchone()[0]
                evidence['callbacks'] = callbacks[:]
                assert evidence['profile']['tag_status'] == 'ok'
                budget_after_profile = budget(http)
                # The same source version must replay its callback without another model call.
                assert http.post(profile_url+'/api/internal/v1/ai/profile/extract-and-pool', json=body).status_code == 202
                deadline = time.monotonic()+10
                while len(callbacks) < 2 and time.monotonic() < deadline:
                    time.sleep(.1)
                assert len(callbacks) == 2 and callbacks[0] == callbacks[1]
                assert budget(http)['attempts'] == budget_after_profile['attempts']
                evidence['duplicateProfileNoNewPaidAttempt'] = True
            chat, chat_url = start('chat-live', 'chat', 'app.main:app', {
                'PYTHONPATH': os.pathsep.join(map(str, [ROOT/'chat', ROOT])), 'STORE_MODE': 'postgres',
                'DATABASE_URL': database['chat']+'?options=-csearch_path%3Dai_chat,public',
                'CHAT_RUNTIME_MODE': 'live', 'CHAT_MODEL': MODEL, 'CHAT_HARNESS_VARIANT': 'stable-v4.12',
                'AI_BUDGET_MODE': 'guarded',
                'AI_SERVICE_TOKEN': token, 'SESSION_SECRET': token, 'QA_ADMIN_PASSWORD': token,
                'DEMO_ACCESS_TOKEN': token, 'COOKIE_SECURE': 'false', 'OPENROUTER_API_KEY': key,
                'SEARCH_URL': search_url, 'SEARCH_TOKEN': token, 'BUDGET_STORE_URL': budget_url, 'STORE_TOKEN': store_token})
            wait(http, chat_url, chat)
            evidence['chatReady'] = http.get(chat_url+'/readyz').json()
            assert evidence['chatReady']['mode'] == 'live'
            cid = str(uuid4())
            base = chat_url+'/v1/chat/conversations/'+cid
            assert http.put(base, json={'senderUserId': 11, 'recipientUserId': 777}).status_code == 201
            products = {p['productId']: p for p in export['data']['products']}
            for case in cases:
                tid = str(uuid4())
                request = {'message': case['message'], 'model': MODEL, 'recipientContext': {
                    'recipientUserId': 777, 'sourceVersion': 1, 'status': 'AVAILABLE',
                    'giftPreference': PREFERENCE, 'dislikedCategories': body['dislikedCategories']}}
                begin = time.monotonic()
                assert http.put(base+'/turns/'+tid, json=request).status_code in (200, 202)
                stream = http.get(base+'/turns/'+tid+'/events?afterEventId=0')
                state = http.get(base+'/turns/'+tid).json()['data']
                with psycopg.connect(database['owner']) as db:
                    receipt = db.execute('SELECT context FROM ai_chat.turns WHERE id=%s', (tid,)).fetchone()[0]
                item = {'request': request, 'response': state, 'sse': stream.text, 'receipt': receipt,
                        'seconds': time.monotonic()-begin}
                evidence['conversations'].append(item)
                if state['status'] != 'COMPLETED':
                    raise AssertionError('Real Chat failed; stop the representative set without automatic paid retry')
                ids = [p['productId'] for block in state['blocks'] if block['type'] == 'cards' for p in block['products']]
                item['checks'] = {'countWithinRequest': len(ids) <= case['maxCount'],
                    'priceWithinRequest': all(products[i]['price'] <= case['maxPrice'] for i in ids),
                    'explicitDislikesPreserved': all(i not in [301, 401]+case.get('excluded', []) for i in ids),
                    'knownIds': all(i in products for i in ids), 'displayedIds': ids}
                if 'expectedMaxFilter' in case:
                    item['checks']['priceChangeContract'] = receipt['lowLatencyState']['publicFilters']['max_price'] == case['expectedMaxFilter']
                if 'expectedIds' in case:
                    item['checks']['semanticSelection'] = ids == case['expectedIds']
                if 'expectedCount' in case:
                    item['checks']['exactSyntheticCount'] = len(ids) == case['expectedCount']
                if 'expectedExcludedProductIds' in case:
                    item['checks']['exclusionLifetime'] = receipt['lowLatencyState']['publicFilters']['exclude_product_ids'] == case['expectedExcludedProductIds']
                previous = budget(http)
                assert http.put(base+'/turns/'+tid, json=request).json()['data'] == state
                assert budget(http)['attempts'] == previous['attempts']
                item['retryNoNewPaidAttempt'] = True
            evidence['passed'] = all(all(v is True for k, v in c['checks'].items() if k != 'displayedIds')
                                     for c in evidence['conversations'])
            # Automated constraints are separate from a human-readable qualitative assessment.
            evidence['qualitativeReviewRequired'] = True
            return evidence
        finally:
            if budget_url:
                try:
                    evidence['budgetAfter'] = budget(http)
                except Exception as exc:
                    evidence['budgetAfterErrorClass'] = type(exc).__name__
            (output/'live-evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
