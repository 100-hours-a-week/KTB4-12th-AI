"""Scripted multi-turn state contracts; real language decisions are tested separately."""
import copy
import json

import pytest

from test_low_latency import PRODUCT, Search, run, text, tool


@pytest.fixture
def cup_catalog(monkeypatch):
    rows = [{**PRODUCT, 'productId': ident, 'name': name, 'price': price}
            for ident, name, price in [(101, '합성 도자기 머그컵', 15000),
                                      (102, '합성 유리 찻잔', 18000),
                                      (103, '합성 보온 머그컵', 24000)]]

    async def search(self, body, snapshot_id):
        self.queries.append(copy.deepcopy(body))
        filters = body['filters']
        hits = [p for p in rows if p['productId'] not in filters.get('excludeProductIds', [])
                and (filters.get('maxPrice') is None or p['price'] <= filters['maxPrice'])]
        return {'snapshotId': snapshot_id, 'hits': copy.deepcopy(hits)}

    async def hydrate(self, ids, snapshot_id):
        self.hydrations.append(ids)
        return {'snapshotId': snapshot_id,
                'products': copy.deepcopy([p for p in rows if p['productId'] in ids])}

    monkeypatch.setattr(Search, 'search', search)
    monkeypatch.setattr(Search, 'get_products', hydrate)


def decision(ident, changes=None):
    return [tool('gift_context', {'queries': ['컵'], 'changes': changes or {}}),
            tool('show_gifts', {'product_ids': [str(ident)]})]


def model_state(sent):
    return json.loads(sent[0]['messages'][0]['content'].split('STATE_JSON\n', 1)[1])


async def test_one_turn_reselection_then_highest_keeps_previous_candidate(cup_catalog):
    first, *_ = await run(decision(103, {'max_price': 30000}), message='컵 하나만 골라줘')
    second, *_ = await run(decision(102), old=first, message='이번에는 다른 것으로 하나 골라줘')
    assert second['lowLatencyState']['publicFilters']['exclude_product_ids'] == []
    assert second['lowLatencyState']['publicFilters']['max_price'] == 30000
    last, sent, _, search, *_ = await run(decision(103, {'max_price': None}), old=second,
                                         message='가격 제한 없이 가장 비싼 컵 하나')
    assert last['selectedProducts'][0]['productId'] == 103
    assert not search.queries[0]['filters']['excludeProductIds']
    assert 'persistent_product_exclusion_origins' not in model_state(sent)


async def test_explicit_exclusion_survives_price_changes_until_cleared(cup_catalog):
    instruction = '보온 머그컵은 앞으로 계속 빼고 3만원 이하 컵 하나'
    first, _, _, _, _, first_tid = await run(
        decision(102, {'max_price': 30000, 'exclude_product_ids': ['103']}), message=instruction)
    second, sent, _, search, *_ = await run(decision(102, {'max_price': None}), old=first,
                                            message='가격 상한만 없애고 가장 비싼 컵 하나')
    assert search.queries[0]['filters']['excludeProductIds'] == [103]
    assert model_state(sent)['persistent_product_exclusion_origins'] == [
        {'product_id': '103', 'user_message': instruction, 'turn_id': first_tid}]
    assert second['lowLatencyState']['publicFilters']['exclude_product_ids'] == ['103']
    last, _, _, search, *_ = await run(decision(103, {'exclude_product_ids': []}), old=second,
                                      message='아까 상품 제외도 해제하고 가장 비싼 컵 하나')
    assert last['selectedProducts'][0]['productId'] == 103
    assert not search.queries[0]['filters']['excludeProductIds']
    assert last['lowLatencyState']['publicFilters']['exclude_product_ids'] == []
    assert last['lowLatencyState']['productExclusionSources'] == {}


async def test_recorded_inferred_exclusion_has_public_origin_and_requires_model_patch(cup_catalog):
    # Reproduce the observed wrong model call. The server must not infer intent
    # from regexes or silently erase a potentially intentional saved constraint.
    first, *_ = await run(decision(102, {'exclude_product_ids': ['103']}),
                         message='다시 한 개만 골라줘')
    remembered, sent, *_ = await run([text('저장된 조건을 확인했어요.')], old=first,
                                     message='현재 조건을 설명해 줘', private=True)
    assert model_state(sent)['persistent_product_exclusion_origins'][0]['user_message'] == '다시 한 개만 골라줘'
    assert remembered['lowLatencyState']['publicFilters']['exclude_product_ids'] == ['103']
    assert 'PRIVATE_CANARY' not in json.dumps(sent)
    corrected, _, _, search, *_ = await run(decision(103, {'exclude_product_ids': []}),
        old=remembered, message='그때는 이번만 다른 것이었어. 이제 가장 비싼 컵 하나')
    assert corrected['selectedProducts'][0]['productId'] == 103
    assert not search.queries[0]['filters']['excludeProductIds']


async def test_mixed_exclusions_keep_each_addition_origin_and_correct_only_b(cup_catalog):
    explicit_a = '보온 머그컵 103은 앞으로 계속 빼줘'
    mistaken_b = '이번에는 다른 것으로 하나 골라줘'
    first, _, _, _, _, first_tid = await run(
        decision(101, {'exclude_product_ids': ['103']}), message=explicit_a, private=True)
    second, _, _, _, _, second_tid = await run(
        decision(102, {'exclude_product_ids': ['103', '101']}), old=first,
        message=mistaken_b, private=True)
    # Deliberately inject the wrong model decision above. The server records
    # additions, not a claim that this wording authorized a persistent ban.
    corrected, sent, _, search, *_ = await run(
        decision(101, {'exclude_product_ids': ['103']}), old=second,
        message='101은 이번만 다른 걸 원한 거야. 103의 지속 제외는 유지해줘', private=True)
    state = model_state(sent)
    assert state.get('persistent_product_exclusion_origins') == [
        {'product_id': '103', 'user_message': explicit_a, 'turn_id': first_tid},
        {'product_id': '101', 'user_message': mistaken_b, 'turn_id': second_tid},
    ], state.get('persistent_product_exclusion_origin')
    assert 'persistent_product_exclusion_origin' not in state
    assert corrected['lowLatencyState']['publicFilters']['exclude_product_ids'] == ['103']
    assert corrected['lowLatencyState']['productExclusionSources'] == {
        '103': {'message': explicit_a, 'turnId': first_tid}}
    assert corrected['selectedProducts'][0]['productId'] == 101
    assert search.queries[0]['filters']['excludeProductIds'] == [103]
    assert all(20 in query['filters']['excludeCategoryIds'] for query in search.queries)
    assert corrected['lowLatencyState']['publicFilters']['exclude_category_ids'] == []
    assert 'PRIVATE_CANARY' not in json.dumps(sent)


async def test_readded_exclusion_gets_new_origin_and_recipient_change_drops_it(cup_catalog):
    first, *_ = await run(decision(102, {'exclude_product_ids': ['103']}),
                         message='103은 앞으로 빼줘')
    cleared, *_ = await run(decision(103, {'exclude_product_ids': []}), old=first,
                           message='제외를 전부 해제해줘')
    readded, _, _, _, _, tid = await run(decision(102, {'exclude_product_ids': ['103']}),
                                        old=cleared, message='103을 다시 계속 제외해줘')
    assert readded['lowLatencyState']['productExclusionSources'] == {
        '103': {'message': '103을 다시 계속 제외해줘', 'turnId': tid}}
    # An old receipt for a different bound recipient must not restore either
    # the public exclusions or their public provenance into this recipient.
    readded['lowLatencyState']['recipientIdentity'] = 'different-recipient'
    changed, sent, _, search, *_ = await run(decision(103), old=readded,
                                           message='이 수신자에게 컵 하나', private=True)
    assert changed['lowLatencyState']['productExclusionSources'] == {}
    assert 'persistent_product_exclusion_origins' not in model_state(sent)
    assert not search.queries[0]['filters']['excludeProductIds']
    assert all(20 in query['filters']['excludeCategoryIds'] for query in search.queries)
    assert changed['lowLatencyState']['publicFilters']['exclude_category_ids'] == []
    assert 'PRIVATE_CANARY' not in json.dumps(sent)


async def test_legacy_aggregate_origin_is_unknown_for_every_retained_item(cup_catalog):
    first, *_ = await run(decision(101, {'exclude_product_ids': ['103']}),
                         message='103은 앞으로 계속 빼줘')
    old, *_ = await run(decision(102, {'exclude_product_ids': ['103', '101']}),
                       old=first, message='이번에는 다른 것으로 하나 골라줘')
    old['lowLatencyState'].pop('productExclusionSources', None)
    # Old receipts have only the most recent field replacement. It is not the
    # origin of every member, even though it includes a real public utterance.
    remembered, sent, *_ = await run([text('어느 상품의 제외를 해제할지 확인해 주세요.')],
                                     old=old, message='기존 제외를 확인해줘', private=True)
    state = model_state(sent)
    assert state.get('persistent_product_exclusion_origins') == [
        {'product_id': '103', 'user_message': None, 'turn_id': None},
        {'product_id': '101', 'user_message': None, 'turn_id': None},
    ], state.get('persistent_product_exclusion_origin')
    assert 'persistent_product_exclusion_origin' not in state
    assert remembered['lowLatencyState']['publicFilters']['exclude_product_ids'] == ['103', '101']
    assert remembered['lowLatencyState']['productExclusionSources'] == {}
    assert 'PRIVATE_CANARY' not in json.dumps(sent)
    corrected, _, _, search, *_ = await run(
        decision(101, {'exclude_product_ids': ['103']}), old=remembered,
        message='101만 제외 해제하고 103은 그대로 유지해줘', private=True)
    assert search.queries[0]['filters']['excludeProductIds'] == [103]
    assert corrected['lowLatencyState']['productExclusionSources'] == {}
    assert all(20 in query['filters']['excludeCategoryIds'] for query in search.queries)
