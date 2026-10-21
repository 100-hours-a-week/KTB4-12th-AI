"""Actual gift tool loop and HTTP search; synthetic provider/storage boundaries."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'chat'), str(ROOT / 'chat/tests')]
for key in ['AI_SERVICE_TOKEN', 'SESSION_SECRET', 'QA_ADMIN_PASSWORD', 'DEMO_ACCESS_TOKEN']:
    os.environ.setdefault(key, 'synthetic-tool-fixture-'+uuid.uuid4().hex)
from app.low_latency import LowLatencyRuntime
from app.model_config import ModelCatalog
from app.search_client import SearchClient
from app.task_budget import BudgetHTTP
from app.gift_core.contracts import Filters
from test_low_latency import Store, tool


async def check():
    profile = json.loads(os.environ['FIXTURE_PROFILE_RESULT'])
    product_id = profile['productIds'][0]
    results = []
    async with SearchClient(os.environ['FIXTURE_SEARCH_URL'], source='chat') as search:
        metadata = await search.metadata()
        for prepared in [False, True]:
            store = Store()
            # Explicit fixture transfer of actual profiler output; no BE GET claim.
            store.supplied = {'recipientUserId':42, 'status':'AVAILABLE',
                              'giftPreference':' '.join(profile['preferredTags']), 'dislikedCategories':[]}
            cid, tid = str(uuid.uuid4()), str(uuid.uuid4())
            await store.call('conversation', cid=cid, sender=7, recipient=42, protocol_fixture=False, demo_profile=None)
            await store.call('start', cid=cid, tid=tid, message='합성 선물 추천')
            chosen_id = product_id
            show = {'message':'자료에서 확인한 후보입니다.', 'products':[{'product_id':str(product_id), 'reason':'자료에서 확인한 후보로 살펴볼 수 있어요.'}]}
            packets = [tool('show_gifts', dict(show, filters=Filters().model_dump()))] if prepared else [
                tool('gift_context', {'queries':['컵'], 'filters':Filters().model_dump(), 'expand_catalog':False}),
                tool('show_gifts', show)]
            sent = []
            def respond(request):
                nonlocal chosen_id
                assert request.url.path == '/chat/completions'
                body = json.loads(request.content)
                if not prepared and sent:
                    observed = json.loads(next(m['content'] for m in reversed(body['messages']) if m['role']=='tool'))
                    chosen_id = int(observed['products'][0]['product_id'])
                    packets[0] = tool('show_gifts', dict(show, products=[dict(show['products'][0], product_id=str(chosen_id))]))
                sent.append(body)
                return httpx.Response(200, text='data: '+json.dumps(packets.pop(0), ensure_ascii=False)+'\n\ndata: [DONE]\n\n')
            async with httpx.AsyncClient(base_url='https://synthetic.invalid', transport=httpx.MockTransport(respond)) as http:
                runtime = object.__new__(LowLatencyRuntime)
                runtime.init_state(store, search, 'synthetic-owner')
                runtime.model = 'openai/gpt-6-luna'
                runtime.catalog = ModelCatalog(http)
                runtime.http = BudgetHTTP(http, store)
                runtime.admissions[tid] = {'prepared_context': {'productIds':profile['productIds'], 'snapshotId':metadata['snapshotId']} if prepared else None}
                receipt = {'startedMonotonic':time.monotonic(), 'usage':[]}
                await runtime.generate(cid, tid, receipt)
                assert receipt['selectedProducts'][0]['productId'] == chosen_id
                assert receipt['snapshotId'] == metadata['snapshotId']
                assert len(sent) == (1 if prepared else 2)
                results.append({'preparedProfilerPool':prepared, 'providerFixtureCalls':len(sent), 'actualSearchSnapshotMatched':True})
    return {'passed':True, 'paidModelCalls':0, 'actualGiftToolLoopAndSearchHTTP':True,
            'actualProfilerOutputTransferredByFixture':True, 'BEProfileGETImplemented':False,
            'chatStoreAndProviderSynthetic':True, 'paths':results}


if __name__ == '__main__':
    print(json.dumps(asyncio.run(check())))
