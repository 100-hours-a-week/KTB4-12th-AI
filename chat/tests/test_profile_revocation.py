"""A denied new-turn snapshot must retire private retrieval, not public history.

The PostgreSQL grant, expiry and revocation checks live in test_recipient_data.
This test starts from their admitted snapshots and scripts every model decision.
"""
import copy
import json
import time
from uuid import uuid4

import httpx

from app.low_latency import LowLatencyRuntime
from app.model_config import ModelCatalog
from test_low_latency import PRODUCT, Search, Store, context, show, text, tool


class ScopedSearch(Search):
    private_product = {
        **PRODUCT,
        "productId": 102,
        "name": "PRIVATE_DERIVED_UNSHOWN_CANDIDATE",
    }

    async def search(self, body, snapshot_id):
        self.queries.append(copy.deepcopy(body))
        products = ([self.private_product] if body["query"] == "PRIVATE_PROFILE_TAG"
                    else [PRODUCT])
        return {"snapshotId": snapshot_id, "hits": products}

    async def get_products(self, ids, snapshot_id):
        self.hydrations.append(ids)
        return {
            "snapshotId": snapshot_id,
            "products": [p for p in (PRODUCT, self.private_product) if p["productId"] in ids],
        }


async def test_revoked_profile_stops_private_retrieval_and_keeps_public_conversation():
    store, search = Store(), ScopedSearch()
    cid = str(uuid4())
    await store.call("conversation", cid=cid, sender=7, recipient=42,
                     protocol_fixture=False, demo_profile=None)
    available = {
        "source": "AI_PROFILE_STORE", "status": "AVAILABLE", "analysisCurrent": True,
        "data": {
            "recipientUserId": 42, "sourceVersion": 1,
            "giftPreference": "PRIVATE_PROFILE_TEXT",
            "dislikedCategories": [{"categoryId": 20, "categoryName": "식품"}],
            "preferredTags": [{"axis": "카테고리", "tag": "PRIVATE_PROFILE_TAG"}],
            "dislikedTags": [{"axis": "브랜드", "tag": "PRIVATE_DISLIKE_TAG"}],
        },
    }
    denied = {
        "source": "AI_PROFILE_STORE", "status": "UNAVAILABLE",
        "accessStatus": "NOT_AUTHORIZED",
    }
    packets, sent = [], []

    def reply(request):
        sent.append(json.loads(request.content))
        packet = packets.pop(0)
        return httpx.Response(200, text="data: " + json.dumps(packet, ensure_ascii=False)
                              + "\n\ndata: [DONE]\n\n")

    receipts, model_turns, search_turns = [], [], []
    turns = [
        (available, "친구에게 2만원 이하 선물 하나 추천해줘",
         [tool("gift_context", context(budget=20000)), tool("show_gifts", show())]),
        (denied, "고마워", [text("도움이 되었길 바라요.")]),
        (denied, "그 예산으로 다시 선물 하나 추천해줘",
         [tool("gift_context", context()), tool("show_gifts", show())]),
    ]
    async with httpx.AsyncClient(base_url="https://synthetic",
                                transport=httpx.MockTransport(reply)) as http:
        runtime = object.__new__(LowLatencyRuntime)
        runtime.init_state(store, search, "owner")
        runtime.model = "openai/gpt-6-luna"
        runtime.catalog = ModelCatalog(http)
        runtime.http = http
        for snapshot, message, replies in turns:
            tid = str(uuid4())
            await store.call("start", cid=cid, tid=tid, message=message)
            store.traces[tid] = {"recipientProfile": copy.deepcopy(snapshot)}
            runtime.admissions[tid] = {"prepared_context": None}
            packets.extend(replies)
            model_start, search_start = len(sent), len(search.queries)
            receipt = {"startedMonotonic": time.monotonic(), "usage": []}
            await runtime.generate(cid, tid, receipt)
            await store.call("context", cid=cid, tid=tid, value=receipt)
            await store.call("emit", cid=cid, tid=tid, kind="turn.finished",
                             payload={"status": "COMPLETED"})
            runtime.admissions.pop(tid)
            receipts.append(receipt)
            model_turns.append(sent[model_start:])
            search_turns.append(search.queries[search_start:])

    assert not packets
    assert [len(requests) for requests in model_turns] == [2, 1, 2]
    assert [[q["query"] for q in queries] for queries in search_turns] == [
        ["PRIVATE_PROFILE_TAG", "책받침"], [], ["책받침"],
    ]
    assert all(20 in q["filters"]["excludeCategoryIds"] for q in search_turns[0])
    assert search_turns[2][0]["filters"]["excludeCategoryIds"] == []
    assert [r["recipientProfile"] for r in receipts[1:]] == [denied, denied]
    assert all(r["lowLatencyState"]["publicFilters"]["max_price"] == 20000
               for r in receipts)
    assert all([p["product_id"] for p in r["lowLatencyState"]["lastDisplayedProducts"]]
               == ["101"] for r in receipts)
    wire = json.dumps(sent, ensure_ascii=False)
    assert all(private not in wire for private in (
        "PRIVATE_PROFILE_TEXT", "PRIVATE_PROFILE_TAG", "PRIVATE_DISLIKE_TAG"))
    # Publicly shown product 101 remains; a private-derived unshown candidate
    # must not survive in the model state, tool history or cached recommendations.
    assert "PRIVATE_DERIVED_UNSHOWN_CANDIDATE" not in json.dumps(model_turns[1:])
