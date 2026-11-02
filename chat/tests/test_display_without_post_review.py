"""Serving uses normal catalog lookup/serialization, without semantic judging."""
import copy

import pytest

import test_low_latency as fixture
from test_card_boundary import public_read
from app.gift_core.provider import ProviderError
from app.gift_core.harness import Harness
from app.gift_browse import BrowseHarness
from app.gift_gallery import GalleryHarness


def choice(ids, reason='없는 규격 9999cm이고 가격은 1원이라는 모델 문장입니다.', operation='show_gifts'):
    value = {'product_ids': ids, 'reasons': [{'product_id': i, 'reason': reason} for i in ids]}
    if operation == 'list_gifts':value['format'] = 'brief'
    return value


@pytest.fixture
def lookup(monkeypatch):
    products = [copy.deepcopy(fixture.PRODUCT),
        {**fixture.PRODUCT, 'productId': 102, 'name': '합성 식품', 'categoryId': 20, 'price': 90000},
        {**fixture.PRODUCT, 'productId': 103}, {**fixture.PRODUCT, 'productId': 104}]
    async def hydrate(self, ids, snapshot_id):
        self.hydrations.append(ids)
        return {'snapshotId': snapshot_id, 'products': [p for p in products if p['productId'] in ids]}
    monkeypatch.setattr(fixture.Search, 'get_products', hydrate)
    return products


@pytest.mark.parametrize('operation', ['show_gifts', 'list_gifts'])
async def test_search_filters_remain_but_output_is_not_semantically_rechecked(operation, lookup, monkeypatch):
    def forbidden(*args, **kwargs):raise AssertionError('Legacy semantic reviewer was called')
    monkeypatch.setattr(Harness, 'validate_show', forbidden)
    monkeypatch.setattr(BrowseHarness, 'validate_list', forbidden)
    monkeypatch.setattr(GalleryHarness, 'validate_brands', forbidden)
    payload = choice(['102'], operation=operation)
    receipt, sent, store, search, cid, tid = await fixture.run([
        fixture.tool('gift_context', fixture.context(budget=10000)), fixture.tool(operation, payload),
    ], private=True, message='하나만 보여줘')
    assert len(sent) == 2 and receipt.get('stages', {}).get('tool_repairs', 0) == 0
    assert search.queries[0]['filters']['maxPrice'] == 10000
    assert search.queries[0]['filters']['excludeCategoryIds'] == [20]
    assert search.hydrations[-1] == [102]
    cards = [p for b in store.states[tid]['blocks'] for p in b.get('products', [])]
    assert cards == [{'productId': 102, 'reason': payload['reasons'][0]['reason']}]
    assert receipt['selectedProducts'][0]['price'] == 90000  # Catalog fact never overwritten by prose.
    await store.call('emit', cid=cid, tid=tid, kind='turn.finished', payload={'status': 'COMPLETED'})
    snapshot, _, events = await public_read(store, cid, tid, monkeypatch)
    assert events[-1]['data']['status'] == 'COMPLETED'
    assert next(b['products'] for b in snapshot['blocks'] if b['type'] == 'cards') == cards


@pytest.mark.parametrize('operation', ['show_gifts', 'list_gifts'])
@pytest.mark.parametrize('ids', [[], ['999'], ['invalid', '-1', '0', '9007199254740992']])
async def test_missing_or_invalid_ids_are_a_normal_empty_display(operation, ids, lookup):
    receipt, sent, store, search, _, tid = await fixture.run([
        fixture.tool('gift_context', fixture.context()), fixture.tool(operation, choice(ids, operation=operation)),
    ])
    assert len(sent) == 2 and not receipt.get('agentFailureCode')
    assert not any(b['type'] == 'cards' for b in store.states[tid]['blocks'])
    assert any(b['type'] == 'text' for b in store.states[tid]['blocks'])
    assert all(i > 0 for batch in search.hydrations for i in batch)


@pytest.mark.parametrize('operation', ['show_gifts', 'list_gifts'])
async def test_duplicates_and_public_card_bound_are_serialized_without_repair(operation, lookup):
    ids = ['101', '101', '102', '103', '104']
    receipt, sent, store, _, _, tid = await fixture.run([
        fixture.tool('gift_context', fixture.context()), fixture.tool(operation, choice(ids, operation=operation)),
    ], message='선물 한 개만 추천해줘')
    cards = [p for b in store.states[tid]['blocks'] for p in b.get('products', [])]
    assert [p['productId'] for p in cards] == [101, 102, 103]
    assert len(sent) == 2 and receipt.get('stages', {}).get('tool_repairs', 0) == 0
    # The existing BE contract caps cards at3; the requested1 is model intent,
    # not a server semantic rule. No third request or generated-failure repair.


async def test_reason_order_is_associated_by_id_not_rejudged(lookup):
    payload = {'product_ids': ['102', '101'], 'reasons': [
        {'product_id': '101', 'reason': '첫 상품 이유'}, {'product_id': '102', 'reason': '둘째 상품 이유'}]}
    _, _, store, _, _, tid = await fixture.run([
        fixture.tool('gift_context', fixture.context()), fixture.tool('show_gifts', payload),
    ])
    cards = [p for b in store.states[tid]['blocks'] for p in b.get('products', [])]
    assert cards == [{'productId': 102, 'reason': '둘째 상품 이유'}, {'productId': 101, 'reason': '첫 상품 이유'}]


async def test_broken_shape_fails_once_without_consuming_a_repair(lookup):
    packets = [fixture.tool('gift_context', fixture.context()),
        fixture.tool('show_gifts', {'product_ids': 'not-an-array', 'reasons': []}),
        fixture.tool('show_gifts', choice(['101']))]
    with pytest.raises(ProviderError):await fixture.run(packets)
    assert len(packets) == 1


async def test_lookup_snapshot_mismatch_remains_a_transport_error(lookup, monkeypatch):
    async def wrong(self, ids, snapshot_id):return {'snapshotId': 'wrong-snapshot', 'products': []}
    monkeypatch.setattr(fixture.Search, 'get_products', wrong)
    with pytest.raises(ProviderError):
        await fixture.run([fixture.tool('gift_context', fixture.context()), fixture.tool('show_gifts', choice(['102']))])
