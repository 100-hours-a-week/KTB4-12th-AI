"""Synthetic recommendation-reason contracts; no natural-language quality claim."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
import test_low_latency as fixture
from test_card_boundary import public_read

from app.gift_adapter import CatalogAdapter, view
from app.gift_clean_io import Selection as LegacySelection
from app.gift_core.contracts import Filters, Profile
from app.gift_core.provider import ProviderError, ScriptedProvider, tool_events
from app.integrated_harness import IntegratedHarness


def test_supplied_recommendation_reason_is_not_replaced_by_catalog_description():
    """Fails on bd0f057: StableHarness replaces this reason with 상품 정보."""
    harness = object.__new__(IntegratedHarness)
    harness.catalog = CatalogAdapter(None, fixture.META, {})
    row = view(fixture.PRODUCT)
    harness.catalog.products[row['product_id']] = row
    reason = '책을 받쳐 두고 읽으려는 요청에 맞는 후보예요.'
    filters = Filters()
    args = SimpleNamespace(
        products=[SimpleNamespace(product_id='101', reason=reason)],
        filters=filters,
        model_dump=lambda: {'product_ids': ['101'], 'reasons': [reason]},
    )
    session = SimpleNamespace(filters=filters, profile=Profile())
    cards, _ = harness.validate_show(args, session, {'101': row})
    assert cards[0]['reason'] == reason
    assert cards[0]['reason'] != '상품 정보: ' + fixture.PRODUCT['description']


@pytest.mark.parametrize('violation', ['filtered', 'unsupported-number', 'wrong-brand'])
def test_model_choices_and_prose_are_not_semantically_reviewed(violation):
    harness = object.__new__(IntegratedHarness)
    harness.catalog = CatalogAdapter(None, fixture.META, {})
    row = view(fixture.PRODUCT)
    other = view({**fixture.PRODUCT, 'productId': 102, 'brand': '별도상표'})
    harness.catalog.products = {'101': row, '102': other}
    observed = {'101': row, '102': other}
    filters = Filters(exclude_product_ids=['101']) if violation == 'filtered' else Filters()
    payload = selection()
    if violation == 'unobserved':
        observed.pop('101')
    elif violation == 'unsupported-number':
        payload['reasons'][0]['reason'] = '크기가 9999cm라 요청한 독서용으로 골랐어요.'
    elif violation == 'wrong-brand':
        payload['reasons'][0]['reason'] = '별도상표 제품이라 요청한 독서용으로 골랐어요.'
    args = harness.show_args(harness.selection_model.model_validate(payload), filters)
    cards, _ = harness.validate_show(args, SimpleNamespace(filters=filters, profile=Profile()), observed)
    assert cards[0]['reason'] == payload['reasons'][0]['reason']



def test_confirmed_numeric_fact_in_model_reason_is_preserved():
    harness = object.__new__(IntegratedHarness)
    harness.catalog = CatalogAdapter(None, fixture.META, {})
    row = view(fixture.PRODUCT)
    harness.catalog.products = {'101': row}
    reason = '10000원으로 예산 안에서 책을 받쳐 읽을 수 있어 골랐어요.'
    payload = selection()
    payload['reasons'][0]['reason'] = reason
    filters = Filters(max_price=10000)
    args = harness.show_args(harness.selection_model.model_validate(payload), filters)
    cards, _ = harness.validate_show(args, SimpleNamespace(filters=filters, profile=Profile()), {'101': row})
    assert cards[0]['reason'] == reason


REASONS = {
    '101': '책을 받쳐 두고 읽으려는 요청에 맞아 골랐어요.',
    '102': '읽던 쪽을 표시하려는 요청에 맞는 책갈피예요.',
    '103': '독서 중 메모를 남기려는 요청에 맞는 수첩이에요.',
}


def selection(ids=('101',), *, operation='show_gifts'):
    payload = {
        'product_ids': list(ids),
        'reasons': [{'product_id': ident, 'reason': REASONS.get(ident, REASONS['101'])}
                    for ident in ids],
    }
    if operation == 'list_gifts':
        payload['format'] = 'brief'
    return payload


@pytest.fixture
def synthetic_catalog(monkeypatch):
    rows = [
        copy.deepcopy(fixture.PRODUCT),
        {**fixture.PRODUCT, 'productId': 102, 'name': '합성 책갈피',
         'brand': '별도상표', 'description': '읽던 쪽에 끼워 두는 합성 책갈피'},
        {**fixture.PRODUCT, 'productId': 103, 'name': '합성 독서 수첩',
         'description': '독서 메모를 적는 합성 수첩'},
    ]

    async def search(self, body, snapshot_id):
        self.queries.append(copy.deepcopy(body))
        return {'snapshotId': snapshot_id, 'hits': copy.deepcopy(rows)}

    async def hydrate(self, ids, snapshot_id):
        self.hydrations.append(list(ids))
        return {'snapshotId': snapshot_id,
                'products': copy.deepcopy([p for p in rows if p['productId'] in ids])}

    monkeypatch.setattr(fixture.Search, 'search', search)
    monkeypatch.setattr(fixture.Search, 'get_products', hydrate)
    return rows


@pytest.mark.parametrize('operation', ['show_gifts', 'list_gifts'])
async def test_distinct_reasons_reach_stored_cards_public_get_and_sse(
        operation, synthetic_catalog, monkeypatch):
    ids = ['102', '101']  # Different from retrieval order; reason association must follow IDs.
    payload = selection(ids, operation=operation)
    payload['message'] = '독서에 사용할 후보를 골랐어요.'
    receipt, sent, store, _, cid, tid = await fixture.run([
        fixture.tool('gift_context', fixture.context()), fixture.tool(operation, payload),
    ], message='책을 받쳐 읽고 읽던 쪽을 표시할 용도로 두 개만 보여줘')
    expected = [{'productId': int(ident), 'reason': REASONS[ident]} for ident in ids]
    assert next(b['products'] for b in store.states[tid]['blocks'] if b['type'] == 'cards') == expected
    assert len(sent) == 2
    assert fixture.LowLatencyRuntime.harness_class(object.__new__(fixture.LowLatencyRuntime)) is IntegratedHarness
    selection_schema = next(t['function']['parameters'] for t in sent[1]['tools']
                            if t['function']['name'] == 'show_gifts')
    assert 'reasons' in selection_schema['required']
    assert selection_schema['properties']['reasons']['items']['properties']['reason']['maxLength'] == 200
    listing_schema = next(t['function']['parameters'] for t in sent[1]['tools']
                          if t['function']['name'] == 'list_gifts')
    assert 'reasons' in listing_schema['properties']
    assert receipt['selectedProducts'][0]['productId'] == 102
    descriptions = {p['description'] for p in synthetic_catalog}
    assert all(p['reason'] not in descriptions and not p['reason'].startswith('상품 정보: ')
               for p in expected)
    await store.call('emit', cid=cid, tid=tid, kind='turn.finished', payload={'status': 'COMPLETED'})
    snapshot, conversation, events = await public_read(store, cid, tid, monkeypatch)
    assert next(b['products'] for b in snapshot['blocks'] if b['type'] == 'cards') == expected
    assert conversation['turns'] == [snapshot]
    assert events[-1]['event'] == 'turn.finished' and events[-1]['data'] == snapshot
    cards_event = next(e for e in events if e['event'] == 'cards')
    assert cards_event['data'] == {'products': expected}
    _, _, resumed = await public_read(store, cid, tid, monkeypatch, after=cards_event['id'])
    assert resumed[-1]['data'] == snapshot


@pytest.mark.parametrize('operation', ['show_gifts', 'list_gifts'])
@pytest.mark.parametrize('count', [1, 2, 3])
async def test_scripted_requested_count_is_preserved_without_padding(
        operation, count, synthetic_catalog):
    # This proves transport cardinality, not real-model interpretation of the request.
    ids = list(REASONS)[:count]
    _, _, store, _, _, tid = await fixture.run([
        fixture.tool('gift_context', fixture.context()),
        fixture.tool(operation, selection(ids, operation=operation)),
    ], message=f'독서에 사용할 상품 {count}개만 보여줘')
    cards = next(b['products'] for b in store.states[tid]['blocks'] if b['type'] == 'cards')
    assert cards == [{'productId': int(ident), 'reason': REASONS[ident]} for ident in ids]
    assert all(set(card) == {'productId', 'reason'} for card in cards)  # Images belong to BE.


def test_active_schema_requires_per_id_reasons_without_changing_legacy_pc_schema():
    harness = object.__new__(IntegratedHarness)
    schema = harness.selection_tool(None, ())['function']['parameters']
    assert {'product_ids', 'reasons'} <= set(schema['required'])
    assert schema['properties']['reasons']['maxItems'] == 3
    item = schema['properties']['reasons']['items']
    assert set(item['required']) == {'product_id', 'reason'}
    assert item['properties']['reason']['minLength'] == 1
    assert item['properties']['reason']['maxLength'] == 200
    assert item['additionalProperties'] is False
    assert 'reasons' not in LegacySelection.model_json_schema()['properties']
    assert LegacySelection.model_validate({'product_ids': ['101']}).product_ids == ['101']
    with pytest.raises(ValidationError):
        harness.parse_selection({'product_ids': ['101']}, schema)
    assert harness.parse_selection(selection([]), schema).product_ids == []


INVALID_SELECTIONS = [
    pytest.param({'product_ids': ['101']}, id='missing-reasons'),
    pytest.param({'reasons': [{'product_id': '101', 'reason': REASONS['101']}]}, id='missing-ids'),
    pytest.param({'product_ids': '["101"]', 'reasons': []}, id='ids-string'),
    pytest.param({'product_ids': [101], 'reasons': selection()['reasons']}, id='nonstring-id'),
    pytest.param({'product_ids': ['101'], 'reasons': {'101': REASONS['101']}}, id='reasons-map'),
    pytest.param({'product_ids': ['101'], 'reasons': [{'product_id': '101'}]}, id='missing-reason-text'),
    pytest.param({'product_ids': ['101'], 'reasons': [
        {'product_id': '101', 'reason': None}]}, id='null-reason'),
    pytest.param({'product_ids': ['101'], 'reasons': [
        {'product_id': '101', 'reason': 123}]}, id='numeric-reason'),
    *[pytest.param({'product_ids': ['101'], 'reasons': [
        {'product_id': '101', 'reason': reason}]}, id=name)
      for name, reason in [('empty-reason', ''), ('ascii-blank', ' \t\r\n '),
                           ('unicode-blank', '\u3000\u00a0'), ('overlong-reason', '가' * 201)]],
    pytest.param({**selection(), 'image': 'invented-image'}, id='unexpected-field'),
    pytest.param({'product_ids': ['101'], 'reasons': [
        {**selection()['reasons'][0], 'image': 'invented-image'}]}, id='unexpected-reason-field'),
]


@pytest.mark.parametrize('payload', INVALID_SELECTIONS)
@pytest.mark.parametrize('operation', ['show_gifts', 'list_gifts'])
def test_invalid_reason_contract_is_rejected_before_display(payload, operation):
    harness = object.__new__(IntegratedHarness)
    payload = copy.deepcopy(payload)
    if operation == 'list_gifts':
        payload['format'] = 'brief'
        validate = harness.listing_model.model_validate
    else:
        validate = harness.selection_model.model_validate
    if operation == 'list_gifts' and 'reasons' not in payload:
        assert validate(payload).reasons == []
    else:
        with pytest.raises(ValidationError):
            validate(payload)


@pytest.mark.parametrize('operation', ['show_gifts', 'list_gifts'])
@pytest.mark.parametrize('length', [1, 200])
def test_reason_length_endpoints_preserve_the_complete_string(operation, length):
    harness = object.__new__(IntegratedHarness)
    payload = selection(operation=operation)
    payload['reasons'][0]['reason'] = '가' * length
    model = harness.selection_model if operation == 'show_gifts' else harness.listing_model
    assert model.model_validate(payload).reasons[0].reason == '가' * length


@pytest.mark.parametrize('operation', ['show_gifts', 'list_gifts'])
async def test_two_hundred_character_reason_reaches_public_response_intact(operation, monkeypatch):
    # Synthetic character boundary only; this is deliberately not a prose-quality assertion.
    reason = '가' * 200
    payload = selection(operation=operation)
    payload['reasons'][0]['reason'] = reason
    _, _, store, _, cid, tid = await fixture.run([
        fixture.tool('gift_context', fixture.context()), fixture.tool(operation, payload),
    ])
    await store.call('emit', cid=cid, tid=tid, kind='turn.finished', payload={'status': 'COMPLETED'})
    snapshot, _, events = await public_read(store, cid, tid, monkeypatch)
    expected = [{'productId': 101, 'reason': reason}]
    assert next(b['products'] for b in snapshot['blocks'] if b['type'] == 'cards') == expected
    assert next(e['data']['products'] for e in events if e['event'] == 'cards') == expected
    assert events[-1]['data'] == snapshot


def test_name_only_listing_does_not_require_reason_pair_review():
    harness = object.__new__(IntegratedHarness)
    assert harness.listing_model.model_validate({'product_ids': ['101']}).reasons == []
    assert harness.listing_model.model_validate(selection()).product_ids == ['101']


@pytest.mark.parametrize('operation', ['show_gifts', 'list_gifts'])
@pytest.mark.parametrize('failure', ['unknown-id', 'unsupported-number', 'other-brand', 'blank', 'overlong'])
async def test_prose_passes_and_unknown_or_malformed_payloads_do_not_repair(
        operation, failure, synthetic_catalog, monkeypatch):
    payload = selection(['999'] if failure == 'unknown-id' else ['101'], operation=operation)
    payload['message'] = '이 문장은 검증 실패 시 공개되면 안 됩니다.'
    if failure != 'unknown-id':
        payload['reasons'][0]['reason'] = {
            'unsupported-number': '폭이 9999cm라 책을 받쳐 읽는 요청에 맞아요.',
            'other-brand': '별도상표 상품이라 책을 받쳐 읽는 요청에 맞아요.',
            'blank': ' \u3000 ', 'overlong': '가' * 201,
        }[failure]
    store = fixture.Store()
    monkeypatch.setattr(fixture, 'Store', lambda: store)
    packets = [fixture.tool('gift_context', fixture.context()), fixture.tool(operation, payload), fixture.tool(operation, payload)]
    if failure in ('blank', 'overlong'):
        with pytest.raises(ProviderError):
            await fixture.run(packets)
    else:
        await fixture.run(packets)
    assert len(packets) == 1  # No semantic or malformed-payload repair request.
    tid, state = next(iter(store.states.items()))
    cards = [p for b in state['blocks'] for p in b.get('products', [])]
    if failure in ('blank', 'overlong', 'unknown-id'):
        assert not cards
    else:
        assert cards[0]['reason'] == payload['reasons'][0]['reason']



@pytest.mark.parametrize('operation', ['show_gifts', 'list_gifts'])
async def test_serving_missing_reason_keeps_two_attempt_limit_and_no_fallback(operation, monkeypatch):
    invalid = {'product_ids': ['101'], 'message': '이 안내는 거부된 호출에만 있어요.'}
    if operation == 'list_gifts':
        invalid['format'] = 'brief'
    packets = [
        fixture.tool('gift_context', fixture.context()), fixture.tool(operation, invalid),
        fixture.tool(operation, selection(operation=operation)),
    ]
    store = fixture.Store()
    monkeypatch.setattr(fixture, 'Store', lambda: store)
    if operation == 'show_gifts':
        with pytest.raises(ProviderError):
            await fixture.run(packets)
    else:
        await fixture.run(packets)
    assert len(packets) == 1  # Existing two-transport-attempt ceiling remains in force.
    assert not any(b['type'] == 'cards' for b in next(iter(store.states.values()))['blocks'])


@pytest.mark.parametrize('operation', ['show_gifts', 'list_gifts'])
async def test_missing_reason_never_requests_a_model_repair(operation):
    from test_compact_harness import Recorded, run
    from test_three_gifts import three_setup

    old, session, _, _, _ = three_setup([])
    invalid = {'product_ids': ['11'], 'message': '이 안내는 거부된 호출에만 있어요.'}
    valid = {'product_ids': ['11'], 'reasons': [
        {'product_id': '11', 'reason': '발에 신는 용도의 선물을 원하셔서 양말을 골랐어요.'}]}
    if operation == 'list_gifts':
        invalid['format'] = valid['format'] = 'brief'
    provider = Recorded([
        tool_events('gift_context', {'queries': ['양말']}),
        tool_events(operation, invalid), tool_events(operation, valid),
    ])
    harness = IntegratedHarness(old.catalog, old.context, provider, turn_timeout=None)
    from app.gift_core.contracts import TurnInput
    events = []
    async def emit(kind,value):events.append((kind,value))
    await harness.run(session, TurnInput(message='양말 하나 보여줘'), 'repair', emit)
    assert len(provider.calls) == 2
    assert harness.metrics['repair'].stages.get('tool_repairs', 0) == 0
    assert not any(kind in ('cards', 'catalog') for kind, _ in events)



async def test_empty_selection_produces_no_fabricated_reason_or_card():
    _, _, store, _, _, tid = await fixture.run([
        fixture.tool('gift_context', fixture.context()), fixture.tool('show_gifts', selection([])),
    ])
    assert not any(b['type'] == 'cards' for b in store.states[tid]['blocks'])
    assert any(b['type'] == 'text' for b in store.states[tid]['blocks'])


async def test_other_bound_recipient_cannot_reuse_prior_reasons_or_candidate_scope():
    first, *_ = await fixture.run([
        fixture.tool('gift_context', fixture.context()), fixture.tool('show_gifts', selection()),
    ])
    first['lowLatencyState']['recipientIdentity'] = 'another-synthetic-recipient'
    second, sent, _, search, *_ = await fixture.run([
        fixture.tool('gift_context', fixture.context()), fixture.tool('show_gifts', selection()),
    ], old=first)
    state = json.loads(sent[0]['messages'][0]['content'].split('STATE_JSON\n', 1)[1])
    assert not state.get('last_products') and not state.get('last_displayed_products')
    assert REASONS['101'] not in json.dumps(sent[0], ensure_ascii=False)
    assert len(search.queries) == 1
    assert second['lowLatencyState']['recipientIdentity'] != 'another-synthetic-recipient'


async def test_cancelled_selection_keeps_previous_reasons_and_no_new_cards():
    from test_three_gifts import three_setup

    old, session, _, _, _ = three_setup([])
    first_reason = '발에 신는 용도의 선물을 원하셔서 양말을 골랐어요.'
    provider = ScriptedProvider([
        tool_events('gift_context', {'queries': ['양말']}),
        tool_events('show_gifts', {'product_ids': ['11'], 'reasons': [
            {'product_id': '11', 'reason': first_reason}]}),
    ])
    harness = IntegratedHarness(old.catalog, old.context, provider, turn_timeout=None)
    request = SimpleNamespace(message='신을 양말 하나 추천해줘', intent_hint='auto', model='fixture')
    events = []

    async def emit(kind, value):
        events.append((kind, copy.deepcopy(value)))

    await harness.run(session, request, 'first', emit)
    assert harness.metrics['first'].status == 'completed'
    before = copy.deepcopy((session.last_products, session.last_displayed_products,
                            session.last_search, session.filters))
    entered = asyncio.Event()

    class WaitForCancellation(ScriptedProvider):
        async def stream(self, messages, tools, model):
            if self.scripts:
                async for event in super().stream(messages, tools, model):
                    yield event
                return
            entered.set()
            await asyncio.Event().wait()
            yield {}  # Make this an async generator; execution cannot reach this.

    harness.provider = WaitForCancellation([
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'max_price': 9000}}),
    ])
    events.clear()
    task = asyncio.create_task(harness.run(session, request, 'cancelled', emit))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    await task
    assert harness.metrics['cancelled'].status == 'cancelled'
    assert (session.last_products, session.last_displayed_products,
            session.last_search, session.filters) == before
    assert session.last_products[0]['reason'] == first_reason
    assert any(kind == 'progress' for kind, _ in events)  # Cancel after a new search.
    assert not any(kind in ('text', 'cards', 'catalog') for kind, _ in events)


@pytest.mark.parametrize('reason',[
    '읽던 쪽을 표시하는 용도이며 5,000원으로 요청한 6,000원 이하입니다.',
    '읽던 쪽을 표시하는 용도이며 가격이 5,000원으로 6,000원 이하입니다.',
    '읽던 쪽을 표시하는 용도이며, 가격도 5,000원으로 0.6만 원 이하입니다.',
    '읽던 쪽을 표시하는 용도이고 예산 6천 원 이내입니다.',
    '읽던 쪽을 표시하고 요청하신 상한 0.6만 원 이하입니다.',
])
def test_current_budget_reference_is_not_a_product_numeric_claim(reason):
    harness=object.__new__(IntegratedHarness)
    harness.catalog=CatalogAdapter(None,fixture.META,{})
    row=view({**fixture.PRODUCT,'price':5000});harness.catalog.products={'101':row}
    filters=Filters(max_price=6000)
    payload=selection();payload['reasons'][0]['reason']=reason
    args=harness.show_args(harness.selection_model.model_validate(payload),filters)
    cards,_=harness.validate_show(args,SimpleNamespace(filters=filters,profile=Profile()),{'101':row})
    assert cards[0]['reason']==reason


@pytest.mark.parametrize('reason',[
    '상품 가격이 6,000원이라 읽던 쪽을 표시하기 좋아요.',
    '배송비가 6,000원 이하라 요청에 맞아요.',
    '배송비도 5,000원으로 6,000원 이하라 요청에 맞아요.',
    '가격도 7,000원으로 6,000원 이하라 요청에 맞아요.',
    '정기 구독료가 6,000원 이내라 요청에 맞아요.',
    '-6,000원 이하라 요청에 맞아요.',
    '예산 - 6,000원 이하라 요청에 맞아요.',
    '예산 6,000원 이하이며 배송비도 6,000원 이하예요.',
    '요청한 배송비 6,000원 이하라 요청에 맞아요.',
    '6,000원 이하가 아닌 고가 상품이에요.',
    '예산 6,000원 이하가 아닙니다.',
    '상품 가격이 6,000원으로 6,000원 이하입니다.',
    '폭이 6,000cm 이하라 읽던 쪽을 표시하기 좋아요.',
    '폭이 6,000cm라 읽던 쪽을 표시하기 좋아요.',
    '예산 7,000원 이하라 읽던 쪽을 표시하기 좋아요.',
    '예산 6,000원 이상이라 읽던 쪽을 표시하기 좋아요.',
    '요청한 6,000원 이하이며 무게는 6,000g입니다.',
    '요청한 6,000원 이하이며 상품 가격은 6,000원입니다.',
])
def test_numeric_reason_text_is_preserved_without_fact_judging(reason):
    harness=object.__new__(IntegratedHarness)
    harness.catalog=CatalogAdapter(None,fixture.META,{})
    row=view({**fixture.PRODUCT,'price':5000});harness.catalog.products={'101':row}
    filters=Filters(max_price=6000)
    payload=selection();payload['reasons'][0]['reason']=reason
    args=harness.show_args(harness.selection_model.model_validate(payload),filters)
    cards, _ = harness.validate_show(args,SimpleNamespace(filters=filters,profile=Profile()),{'101':row})
    assert cards[0]['reason'] == payload['reasons'][0]['reason']


@pytest.mark.parametrize('filters',[Filters(),Filters(max_price=7000),Filters(min_price=6000)])
def test_budget_prose_is_not_compared_to_current_filters(filters):
    harness=object.__new__(IntegratedHarness)
    harness.catalog=CatalogAdapter(None,fixture.META,{})
    row=view({**fixture.PRODUCT,'price':5000});harness.catalog.products={'101':row}
    reason='요청한 6,000 원 이하 예산에 맞는 책받침이에요.'
    payload=selection();payload['reasons'][0]['reason']=reason
    args=harness.show_args(harness.selection_model.model_validate(payload),filters)
    cards, _ = harness.validate_show(args,SimpleNamespace(filters=filters,profile=Profile()),{'101':row})
    assert cards[0]['reason'] == payload['reasons'][0]['reason']


def test_numeric_efficacy_prose_is_not_judged():
    harness=object.__new__(IntegratedHarness)
    harness.catalog=CatalogAdapter(None,fixture.META,{})
    row=view({**fixture.PRODUCT,'price':5000});harness.catalog.products={'101':row}
    filters=Filters(max_price=6000)
    payload=selection();payload['reasons'][0]['reason']='예산 6,000원 이하이고 집중력이 100% 향상돼요.'
    args=harness.show_args(harness.selection_model.model_validate(payload),filters)
    cards, _ = harness.validate_show(args,SimpleNamespace(filters=filters,profile=Profile()),{'101':row})
    assert cards[0]['reason'] == payload['reasons'][0]['reason']
