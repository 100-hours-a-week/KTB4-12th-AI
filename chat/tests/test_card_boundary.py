"""Synthetic regression for the external 200-character card boundary; no DB/provider."""
import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
import test_low_latency as fixture
from app import main
from app.card_contract import catalog_reason, readable_turn, UNREADABLE_CARDS
from app.gift_core.provider import ProviderError
from app.integrated_harness import IntegratedHarness
from app.models import CardsBlock, Turn
from app.runtime import Runtime
from app.store import Store


@pytest.mark.parametrize('products', [[], [{'product_id': '101', 'name': '합성 상품'}]])
def test_public_filter_projection_does_not_claim_private_policy_is_unapplied(products):
    harness = object.__new__(IntegratedHarness)
    context = {'products': products, 'filters': {'max_price': 100, 'exclude_category_ids': ['private-id']}}
    result = harness.model_context(context)
    assert result['filters'] == {'max_price': 100}
    assert result['server_constraints'] == {'recipient_category_policy': 'enforced_for_current_scope'}
    assert 'private-id' not in json.dumps(result)
    assert '현재 적용 조건 전체' not in harness.system_prompt(None, None)


def test_failed_retrieval_never_claims_server_policy_success():
    harness = object.__new__(IntegratedHarness)
    assert 'server_constraints' not in harness.model_context({'error': 'unavailable'})


def install_store(monkeypatch, store):
    runtime = object.__new__(Runtime)
    runtime.init_state(store, None, 'synthetic-owner')
    monkeypatch.setattr(main.app.state, 'store', store, raising=False)
    monkeypatch.setattr(main.app.state, 'runtime', runtime, raising=False)


def frames(text):
    result = []
    for block in text.split('\n\n'):
        lines = dict(line.split(': ', 1) for line in block.splitlines() if ': ' in line)
        if 'event' in lines:
            result.append({'id': int(lines['id']), 'event': lines['event'], 'data': json.loads(lines['data'])})
    return result


async def public_read(store, cid, tid, monkeypatch, after=0):
    install_store(monkeypatch, store)
    async with httpx.AsyncClient(base_url='http://synthetic',
            transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False)) as client:
        headers = {'Authorization': 'Bearer ' + main.TOKEN}
        turn = await client.get(main.BASE + '/' + cid + '/turns/' + tid, headers=headers)
        conversation = await client.get(main.BASE + '/' + cid, headers=headers)
        replay = await client.get(main.BASE + '/' + cid + '/turns/' + tid + '/events',
                                  params={'afterEventId': after}, headers=headers)
    assert (turn.status_code, conversation.status_code, replay.status_code) == (200, 200, 200)
    return turn.json()['data'], conversation.json()['data'], frames(replay.text)


@pytest.mark.parametrize('operation', ['show_gifts', 'list_gifts'])
async def test_complete_short_fact_and_public_get_replay(operation, monkeypatch):
    description = '합성 상품 설명입니다. ' * 25 + '특정 옵션에만 해당합니다.'
    monkeypatch.setitem(fixture.PRODUCT, 'description', description)
    args = {'product_ids': ['101'], **({'format': 'brief'} if operation == 'list_gifts' else {})}
    receipt, _, store, _, cid, tid = await fixture.run([
        fixture.tool('gift_context', fixture.context()), fixture.tool(operation, args)])
    await store.call('emit', cid=cid, tid=tid, kind='turn.finished', payload={'status': 'COMPLETED'})
    card = next(b for b in store.states[tid]['blocks'] if b['type'] == 'cards')
    CardsBlock.model_validate(card)
    assert card['products'] == [{'productId': 101, 'reason': '상품 정보: ' + fixture.PRODUCT['name']}]
    assert receipt['selectedProducts'][0]['description'] == description
    snapshot, conversation, events = await public_read(store, cid, tid, monkeypatch)
    assert conversation['turns'] == [snapshot]
    assert events[-1]['data'] == snapshot
    assert next(e['data'] for e in events if e['event'] == 'cards') == {'products': card['products']}


@pytest.mark.parametrize('field', ['description', 'name', 'brand', 'price'])
def test_reason_chooses_whole_fact_without_truncating_qualifications(field):
    product = {'description': '긴 설명 ' * 100 + '일부 옵션만', 'name': '긴 이름 ' * 100,
               'brand': '긴 브랜드 ' * 100, 'price': 12345}
    if field != 'price':
        product[field] = '완전한 원문이며 특정 옵션에만 해당합니다.'
    before = copy.deepcopy(product)
    reason, evidence = catalog_reason(product)
    assert evidence['field'] == field and len(reason) <= 200 and product == before
    assert reason.endswith(product[field]) if field != 'price' else reason == '상품 가격: 12,345원'


@pytest.mark.parametrize('length, expected_field', [(193, 'description'), (194, 'name')])
def test_reason_limit_includes_the_prefix(length, expected_field):
    reason, evidence = catalog_reason({'description': '가' * length, 'name': '합성 이름'})
    assert len(reason) <= 200 and evidence['field'] == expected_field


def test_valid_snapshot_preserves_existing_block_boundaries():
    state = {'turnId': '00000000-0000-4000-8000-000000000001', 'status': 'COMPLETED',
             'message': '합성 질문', 'lastEventId': 3,
             'blocks': [{'type': 'text', 'text': '첫 문단'}, {'type': 'text', 'text': '둘째 문단'}]}
    assert readable_turn(state) == state


@pytest.mark.parametrize('operation', ['show_gifts', 'list_gifts'])
@pytest.mark.parametrize('invalid_reason', ['x' * 201, '   ', '\t\r\n', '\u3000\u00a0'])
async def test_invalid_preparation_never_commits_intro_or_cards(operation, invalid_reason, monkeypatch):
    store = fixture.Store()
    monkeypatch.setattr(fixture, 'Store', lambda: store)
    monkeypatch.setattr(IntegratedHarness, 'explanation', lambda *_: (invalid_reason, {'field': 'synthetic'}))
    args = {'product_ids': ['101'], **({'format': 'brief'} if operation == 'list_gifts' else {})}
    with pytest.raises(ProviderError):
        await fixture.run([fixture.tool('gift_context', fixture.context()), fixture.tool(operation, args)])
    tid, state = next(iter(store.states.items()))
    assert not state['blocks']
    assert not any(e['event'] in ('cards', 'answer.delta') for e in store.events[tid])
    await store.call('emit', cid=state['_cid'], tid=tid, kind='turn.finished',
                     payload={'status': 'FAILED', 'error': {'message': '합성 표시 검증 실패'}})
    snapshot, _, _ = await public_read(store, state['_cid'], tid, monkeypatch)
    assert snapshot['status'] == 'FAILED' and snapshot['blocks'] == []


@pytest.mark.parametrize('blank', ['   ', '\t\r\n', '\u3000\u00a0'])
def test_legacy_pick_already_rejects_whitespace_before_public_cards(blank):
    from app.gift_core.contracts import Pick
    with pytest.raises(ValidationError):
        Pick(product_id='101', reason=blank)
    assert Pick(product_id='101', reason='  합성 컵을 담는 용도예요.  ').reason=='합성 컵을 담는 용도예요.'
    with pytest.raises(ValidationError):
        Pick(product_id='101', reason='합성 상품 2개예요.')


@pytest.mark.parametrize('reason', ['x' * 200, 'x' * 201])
def test_real_store_boundary_validates_before_mutating_state(reason):
    state = {'status': 'PROCESSING', 'blocks': [], 'lastEventId': 0}
    store = object.__new__(Store)
    store.pool = SimpleNamespace(connection=lambda: nullcontext(None))
    store._guard = lambda *_: None
    store._turn = lambda *_: {'state': state}
    writes = []
    store._event = lambda *args: writes.append(copy.deepcopy(args[2:]))
    payload = {'products': [{'productId': 101, 'reason': reason}]}
    if len(reason) > 200:
        with pytest.raises(ValidationError):
            store.emit('synthetic-cid', 'synthetic-tid', 'cards', payload, 'owner')
        assert state == {'status': 'PROCESSING', 'blocks': [], 'lastEventId': 0} and not writes
    else:
        assert store.emit('synthetic-cid', 'synthetic-tid', 'cards', payload, 'owner')
        CardsBlock.model_validate(state['blocks'][0])
        assert len(writes) == 1


async def test_legacy_invalid_card_read_is_explicit_consistent_and_never_writes(monkeypatch):
    _, _, store, _, cid, tid = await fixture.run([
        fixture.tool('gift_context', fixture.context()), fixture.tool('show_gifts', fixture.show())])
    bad = '상품 정보: ' + '합성 상품 설명입니다. ' * 25
    next(b for b in store.states[tid]['blocks'] if b['type'] == 'cards')['products'][0]['reason'] = bad
    card_event = next(e for e in store.events[tid] if e['event'] == 'cards')
    card_event['data']['products'][0]['reason'] = bad
    await store.call('emit', cid=cid, tid=tid, kind='turn.finished', payload={'status': 'COMPLETED'})
    before = copy.deepcopy((store.states, store.events, store.traces))
    snapshot, conversation, events = await public_read(store, cid, tid, monkeypatch)
    Turn.model_validate(snapshot)
    assert conversation['turns'] == [snapshot] and events[-1]['data'] == snapshot
    assert not any(b['type'] == 'cards' for b in snapshot['blocks'])
    assert UNREADABLE_CARDS in snapshot['blocks'][0]['text']
    replacement = next(e for e in events if e['id'] == card_event['id'])
    assert replacement == {'id': card_event['id'], 'event': 'answer.delta', 'data': {'delta': UNREADABLE_CARDS}}
    _, _, resumed = await public_read(store, cid, tid, monkeypatch, after=card_event['id'])
    assert resumed[-1]['data'] == snapshot
    assert (store.states, store.events, store.traces) == before
