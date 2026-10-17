"""Synthetic private canaries across selection, tools, legacy answers and memory."""
import copy
import json

import pytest

from app.agent import AgentRuntime
from app.runtime import Runtime
from app.public_answer import PUBLIC_ANSWER_VERSION, answer_messages, previous_public_products

CANARY = 'PRIVATE_CANARY_38a1'
PUBLIC_REQUEST = '3만원 이하 선물을 추천해줘'
PRODUCT = {'productId': 2, 'name': '공개 상품', 'price': 20000, 'brand': '공개 브랜드',
           'description': '공개 카탈로그 설명', 'category': '음료', 'categoryId': 24,
           'recipientContext': CANARY, 'preferredTags': [CANARY], 'reason': CANARY,
           'retrievedFor': [CANARY]}


class Store:
    def __init__(self):
        self.turns = [
            {'turnId': 'legacy', 'status': 'COMPLETED', 'message': '이전에 밝힌 공개 조건', 'blocks': [{'type': 'text', 'text': CANARY}]},
            {'turnId': 'current', 'status': 'PROCESSING', 'message': PUBLIC_REQUEST, 'blocks': []},
        ]
        self.contexts = {'legacy': {'selectedProducts': [{**PRODUCT, 'description': CANARY}],
                                   'plan': {'query': CANARY, 'clarification': CANARY},
                                   'conversationMemory': {'version': 1, 'entries': [{'turnId': 'legacy', 'text': CANARY}]}}}
        self.events = []
    async def call(self, method, **args):
        if method == 'recipient_context': return {'recipientUserId': 42, 'status': 'AVAILABLE', 'sourceVersion': 1, 'giftPreference': CANARY, 'dislikedCategories': []}
        if method == 'read': return {'turns': copy.deepcopy(self.turns)}
        if method == 'context':
            if 'value' in args: self.contexts[args['tid']] = copy.deepcopy(args['value'])
            return copy.deepcopy(self.contexts.get(args['tid'], {}))
        if method == 'emit': self.events.append((args['kind'], copy.deepcopy(args['payload']))); return True
        raise AssertionError(method)


class Search:
    async def metadata(self): return {'snapshotId': 'public-catalog', 'categories': [{'id': 24, 'name': '음료', 'group': '카페', 'parentId': 3}]}
    async def search(self, request, **kwargs): return {'snapshotId': 'public-catalog', 'hits': [copy.deepcopy(PRODUCT)]}


@pytest.mark.parametrize('runtime_type', [Runtime, AgentRuntime])
@pytest.mark.parametrize('needs_search', [False, True])
async def test_private_inputs_remain_in_selection_but_never_in_public_generation(runtime_type, needs_search):
    store = Store(); runtime = runtime_type.__new__(runtime_type)
    runtime.init_state(store, Search(), 'fixture'); runtime.model = 'offline-fixture'
    private_prompts, public_requests = [], []
    async def complete(messages, usage, structured, **kwargs):
        private_prompts.append(messages)
        assert CANARY in json.dumps(messages, ensure_ascii=False)
        if structured.__name__ == 'Selection': return {'products': [{'productId': 2, 'reason': CANARY}]}
        return {'search': needs_search, 'query': CANARY, 'filters': {'maxPrice': 30000},
                **({'clarification': CANARY, 'remember': [1, 2]} if structured.__name__ == 'SearchPlan' else {})}
    async def stream(cid, tid, receipt, request):
        public_requests.append(copy.deepcopy(request))
        assert CANARY not in json.dumps(request, ensure_ascii=False)
        assert PUBLIC_REQUEST in json.dumps(request, ensure_ascii=False)
        assert all(message['role'] != 'assistant' for message in request['messages'])
        await runtime.emit(cid, tid, 'answer.delta', {'delta': '공개 사실로 만든 답변'})
    runtime.complete = complete; runtime.stream_answer = stream
    await runtime.run('conversation', 'current')
    assert store.events[-1] == ('turn.finished', {'status': 'COMPLETED'})
    assert len(public_requests) == 1 and len(private_prompts) == (2 if needs_search else 1)
    trace = store.contexts['current']
    assert trace['recipientProfile']['data']['giftPreference'] == CANARY
    assert trace['publicAnswer']['version'] == PUBLIC_ANSWER_VERSION
    assert CANARY not in json.dumps(trace['publicAnswer'], ensure_ascii=False)
    if needs_search:
        cards = next(data for kind, data in store.events if kind == 'cards')
        assert cards['products'][0]['productId'] == 2
        assert CANARY not in json.dumps(cards, ensure_ascii=False)
    else:
        assert trace['publicAnswer']['products'] == []  # Never trust legacy QA product text.


def test_public_memory_requires_original_user_provenance_and_omits_assistant_text():
    turns = Store().turns
    forged = [{'turnId': 'legacy', 'source': 1, 'text': CANARY}]
    messages = answer_messages('Public instructions', turns, 'current', [], False, forged)
    assert CANARY not in json.dumps(messages, ensure_ascii=False)
    assert all(message['role'] != 'assistant' for message in messages)
    original = [{'turnId': 'legacy', 'source': 1, 'text': turns[0]['message']}]
    assert turns[0]['message'] in answer_messages('Public instructions', turns, 'current', [], False, original)[0]['content']


def test_only_new_public_product_cache_is_reused_and_extra_fields_are_removed():
    assert previous_public_products({'selectedProducts': [PRODUCT]}) == []
    context = {'publicAnswer': {'version': PUBLIC_ANSWER_VERSION, 'products': [PRODUCT]}}
    public = previous_public_products(context)
    assert public[0]['productId'] == 2 and public[0]['description'] == PRODUCT['description']
    assert CANARY not in json.dumps(public, ensure_ascii=False)


async def test_private_model_validation_detail_is_not_written_to_general_logs(caplog):
    runtime = Runtime.__new__(Runtime); runtime.init_state(Store(), None, 'fixture'); runtime.model = 'offline'
    async def fail(*args): raise ValueError(CANARY)
    runtime.generate = fail
    await runtime.run('conversation', 'current')
    assert CANARY not in caplog.text
