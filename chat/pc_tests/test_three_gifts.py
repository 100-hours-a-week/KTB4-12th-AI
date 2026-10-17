"""Three-card protocol tests; deterministic fixtures do not measure model quality."""
import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_guided_harness import setup, PRODUCT
from app.gift_core.contracts import Filters
from app.gift_core.provider import tool_events
from app.gift_protocol import DEFAULT, harness_class, manifest
from app.gift_validated import ThreeGiftHarness

from stable_supervision import VerifiedStableProvider, VERSION
from training_contract import validate_records
from evaluation_cases import validate_cases


def three_setup(scripts):
    old, session, search, provider, events, _ = setup(scripts, Filters(max_price=20000))
    products = [{**PRODUCT, 'productId': i, 'name': f'면 혼방 양말 상품 {i}', 'price': i * 500}
                for i in range(11, 15)]
    async def query(request, snapshot_id):
        search.calls.append(request)
        return {'snapshotId': snapshot_id, 'hits': copy.deepcopy(products)}
    async def lookup(ids, snapshot_id):
        return {'snapshotId': snapshot_id, 'products': [copy.deepcopy(p) for p in products if p['productId'] in ids]}
    search.search = query
    search.get_products = lookup
    h = ThreeGiftHarness(old.catalog, old.context, provider, turn_timeout=None)
    async def run(message, tid):
        async def emit(kind, data): events.append((kind, copy.deepcopy(data)))
        await h.run(session, SimpleNamespace(message=message, intent_hint='auto', model='fixture'), tid, emit)
    return h, session, search, events, run


@pytest.mark.asyncio
@pytest.mark.parametrize('ids', [[], ['13'], ['13', '11'], ['13', '11', '12']])
async def test_zero_to_three_selections_keep_display_order_and_history(ids):
    h, s, search, events, run = three_setup([
        tool_events('gift_context', {'queries': ['양말']}),
        tool_events('show_gifts', {'product_ids': ids}),
        tool_events('recall_gift', {'position': 3}),
    ])
    await run('조건에 맞는 양말 세 개까지 추천해줘.', 'recommend')
    assert h.metrics['recommend'].status == 'completed'
    cards = [p for k, v in events if k == 'cards' for p in v['products']]
    assert [p['product_id'] for p in cards] == ids
    assert [p['product_id'] for p in s.last_products] == ids
    assert all(p['evidence']['serverRendered'] for p in cards)
    result = json.loads([m for m in s.history if m['role'] == 'tool'][-1]['content'])
    assert [p['product_id'] for p in result['products']] == ids
    if len(ids) == 3:
        events.clear()
        await run('세 번째 상품 이름과 가격만 다시 알려줘.', 'recall')
        assert h.metrics['recall'].status == 'completed' and len(search.calls) == 1
        assert not any(k == 'cards' for k, _ in events)
        recalled = json.loads([m for m in s.history if m['role'] == 'tool'][-1]['content'])['text']
        assert cards[2]['name'] in recalled and '6,000원' in recalled


@pytest.mark.asyncio
@pytest.mark.parametrize('ids', [['11', '12', '13', '14'], ['11', '12', '11'], ['11', '12', 'missing']])
async def test_four_duplicate_or_unobserved_choices_fail_without_committing(ids):
    invalid = tool_events('show_gifts', {'product_ids': ids})
    h, s, search, events, run = three_setup([
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'max_price': 10000}}), invalid, invalid,
    ])
    await run('만원 이하 양말을 골라줘.', 'bad')
    assert h.metrics['bad'].status == 'failed' and len(search.calls) == 1
    assert s.filters.max_price == 20000 and not s.last_products
    assert not any(k == 'cards' for k, _ in events)


def test_current_limit_and_historical_two_card_contracts_remain_distinct(monkeypatch):
    monkeypatch.delenv('GIFT_HARNESS', raising=False)
    assert VERSION == DEFAULT == 'stable-v4.12'
    assert issubclass(harness_class(), ThreeGiftHarness)
    h, s, _, _, _ = three_setup([])
    for version, maximum, word in [('stable-v4.1', 2, '두'), ('stable-v4.2', 3, '세')]:
        harness = harness_class(version)(h.catalog, h.context, h.provider)
        schema = harness.selection_tool(s, ['11', '12', '13'])
        assert schema['function']['parameters']['properties']['product_ids']['maxItems'] == maximum
        assert f'최대 {word} ID' in harness.system_prompt(s, None)
        assert f'최대 {word} 개' in harness.selection_contract()['arguments']['product_ids']
        with pytest.raises(ValidationError):
            harness.parse_selection({'product_ids': [str(i) for i in range(maximum + 1)]}, schema)


@pytest.mark.asyncio
async def test_training_oracle_and_validator_support_three_ids_and_third_recall():
    provider = VerifiedStableProvider()
    provider.turn = {'expect': {'action': 'recommend', 'count': 3, 'allowed_ids': ['11', '12', '13']}}
    context = {'products': [{'product_id': str(i), 'price': 5000, 'brand': 'A'} for i in range(11, 15)]}
    _ = [event async for event in provider.stream([{'role': 'tool', 'content': json.dumps(context)}],
                                                 [{'function': {'name': 'show_gifts'}}], 'fixture')]
    decision = provider.calls[-1]['completion'][0]
    assert decision['tool_calls'][0]['function']['arguments'] == {'product_ids': ['11', '12', '13']}
    protocol = manifest('stable-v4.2')
    row = {**provider.calls[-1], 'harnessVersion': protocol['version'], 'harnessSha256': protocol['sourceSha256'],
           'serverRenderedTextTarget': False}
    assert validate_records([row], {'harness': protocol}) == protocol
    old = manifest('stable-v4.1')
    with pytest.raises(ValidationError):
        validate_records([{**row, 'harnessVersion': old['version'], 'harnessSha256': old['sourceSha256']}])
    provider.turn = {'expect': {'action': 'text', 'fact_index': 2}, 'derive_last_fact': True}
    _ = [event async for event in provider.stream([], [{'function': {'name': 'recall_gift'}}], 'fixture')]
    decision = provider.calls[-1]['completion'][0]
    assert decision['content'] is None
    assert decision['tool_calls'][0]['function']['arguments'] == {'position': 3}
    assert validate_records([{**row, 'completion': [decision],
                              'tools': provider.calls[-1]['tools']}]) == protocol


def test_evaluation_fixture_accepts_three_cards_and_rejects_four():
    case = {'id': 'three-products', 'split': 'development', 'turns': [{'expect': {
        'action': 'recommend', 'min_count': 3, 'max_count': 3, 'count': 3,
        'filters': Filters().model_dump(), 'allowed_ids': ['11', '12', '13'],
    }}]}
    validate_cases([case])
    case['turns'][0]['expect']['max_count'] = 4
    with pytest.raises(ValueError, match='card count'): validate_cases([case])
