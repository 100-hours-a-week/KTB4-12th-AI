"""Reproduce reported list/card confusion with synthetic catalog facts."""
import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_three_gifts import three_setup
from app.gift_browse import BrowseHarness, BrowseContext, CatalogList
from app.gift_core.provider import tool_events
from app.gift_protocol import manifest
from training_contract import validate_records
from decision_masking import omitted_prose_spans


def browse(scripts):
    old, s, search, events, _ = three_setup(scripts)
    h = BrowseHarness(old.catalog, old.context, old.provider, turn_timeout=None)
    assert isinstance(h.context, BrowseContext)
    async def run(message, tid):
        events.clear()
        async def emit(k, v): events.append((k, copy.deepcopy(v)))
        await h.run(s, SimpleNamespace(message=message, intent_hint='auto', model='fixture'), tid, emit)
        return h.metrics[tid]
    return h, s, search, events, run


@pytest.mark.asyncio
@pytest.mark.parametrize('format', ['names', 'brief'])
async def test_fresh_search_can_end_in_four_item_list_without_cards(format):
    h, s, search, events, run = browse([
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'max_price': 10000}}),
        tool_events('list_gifts', {'product_ids': ['14', '11', '12', '13'], 'format': format}),
    ])
    assert (await run('네 개를 목록으로 보여줘.', 'list')).status == 'completed'
    assert len(search.calls) == 1 and not s.last_products
    assert not any(k == 'cards' for k, _ in events)
    text = ''.join(v['delta'] for k, v in events if k == 'text')
    assert [line.split('. ', 1)[1] for line in text.splitlines() if line[:1].isdigit()][0].startswith('면 혼방 양말 상품 14')
    assert ('상품 정보: ' in text) == (format == 'brief')
    assert ('7,000원' in text) == (format == 'brief')
    assert s.filters.max_price == 10000 and len(s.last_search['products']) == 4
    result = json.loads([m for m in s.history if m['role'] == 'tool'][-1]['content'])
    assert result['source'] == 'last_search' and result['serverRendered']
    assert result['product_ids'] == ['14', '11', '12', '13']


@pytest.mark.asyncio
async def test_unselected_candidates_survive_text_turns_and_reload_without_search():
    h, s, search, events, run = browse([
        tool_events('gift_context', {'queries': ['양말']}),
        tool_events('show_gifts', {'product_ids': ['11', '12', '13']}),
        *[[{'kind': 'text', 'value': '안녕하세요.'}, {'kind': 'finish', 'value': 'stop'}] for _ in range(5)],
        tool_events('list_gifts', {'product_ids': ['14'], 'format': 'names'}),
        tool_events('recall_gift', {'position': 3}),
    ])
    await run('양말 세 개 추천해줘.', 'cards')
    for i in range(5): await run('안녕', 'text' + str(i))
    # The old search tool turn has already left the bounded history.
    assert not any(m.get('role') == 'tool' for m in s.history)
    s.last_search = json.loads(json.dumps(s.last_search))
    assert (await run('아까 나머지 상품명도 나열해줘.', 'rest')).status == 'completed'
    assert len(search.calls) == 1
    text = ''.join(v['delta'] for k, v in events if k == 'text')
    assert text == '1. 면 혼방 양말 상품 14'
    state = json.loads(h.provider.calls[-1]['messages'][0]['content'].split('STATE_JSON\n')[1])
    assert len(state['last_search']['products']) == 4
    assert len(state['last_products']) == 3
    assert s.last_search['last_listed_product_ids'] == ['14']
    assert (await run('추천 카드의 세 번째 이름과 가격', 'recall')).status == 'completed'
    assert '6,500원' in ''.join(v['delta'] for k, v in events if k == 'text')
    assert len(search.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('ids', [['11', 'missing'], ['11', '11']])
async def test_failed_listing_does_not_commit_new_search_or_filters(ids):
    bad = tool_events('list_gifts', {'product_ids': ids})
    h, s, search, events, run = browse([
        tool_events('gift_context', {'queries': ['양말']}), tool_events('show_gifts', {'product_ids': ['11']}),
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'max_price': 10000}}), bad, bad,
    ])
    await run('먼저 하나 골라줘.', 'ok')
    original = copy.deepcopy(s.last_search)
    assert (await run('다른 목록 보여줘.', 'bad')).status == 'failed'
    assert s.last_search == original and s.filters.max_price == 20000
    assert not any(k in ('text', 'cards') for k, _ in events)


@pytest.mark.asyncio
async def test_stale_snapshot_and_changed_recipient_cannot_expose_cached_candidates():
    h, s, _, _, run = browse([
        tool_events('gift_context', {'queries': ['양말']}), tool_events('show_gifts', {'product_ids': ['11']}),
    ])
    await run('하나 추천', 'ok')
    assert h.cached_search(s)
    h.catalog.snapshot = 'different'
    assert h.state(s, SimpleNamespace(intent_hint='auto'), None)['last_search'] is None
    assert 'list_gifts' not in [t['function']['name'] for t in h.answer_tools(s)]
    h.catalog.snapshot = 'test'
    s.profile = s.profile.model_copy(update={'recipient_id': 'another-person'})
    assert h.cached_search(s) is None


@pytest.mark.asyncio
async def test_text_after_search_is_repaired_to_listing_not_forced_cards():
    h, s, _, events, run = browse([
        tool_events('gift_context', {'queries': ['양말']}),
        [{'kind': 'text', 'value': '네 가지 모델명을 적을게요.'}, {'kind': 'finish', 'value': 'stop'}],
        tool_events('list_gifts', {'product_ids': ['11', '12', '13', '14']}),
    ])
    m = await run('모델명 네 개만 나열해줘.', 'list')
    assert m.status == 'completed' and m.stages['tool_repairs'] == 1
    assert 'list_gifts' in h.provider.calls[-1]['messages'][-1]['content']
    assert not any(k == 'cards' for k, _ in events)
    assert '네 가지 모델명을 적을게요.' not in ''.join(v['delta'] for k, v in events if k == 'text')


@pytest.mark.asyncio
async def test_brand_feedback_identifies_exact_field_and_rejected_value():
    h, s, search, events, run = browse([
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'brands': ['라코스톄']}}),
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'brands': ['라코스테']}}),
        tool_events('show_gifts', {'product_ids': ['11']}),
    ])
    assert (await run('라코스테 양말', 'typo')).status == 'completed'
    assert len(search.calls) == 1 and search.calls[0]['filters']['brands'] == ['라코스테']
    feedback = next(v['feedback'] for k, v in events if k == 'tool_repair')
    assert feedback['invalid_fields'] == [{'field': 'changes.brands[0]', 'received': '라코스톄', 'catalog_candidates': ['라코스테']}]


@pytest.mark.asyncio
async def test_search_repair_does_not_consume_terminal_format_repair():
    h, s, search, events, run = browse([
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'brands': ['라코스톄']}}),
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'brands': ['라코스테']}}),
        [{'kind': 'text', 'value': 'No suitable product.'}, {'kind': 'finish', 'value': 'stop'}],
        tool_events('show_gifts', {'product_ids': []}),
    ])
    metrics = await run('라코스테의 다른 종류 상품', 'double-repair')
    assert metrics.status == 'completed' and metrics.calls == 4 and metrics.stages['tool_repairs'] == 2
    assert len(search.calls) == 1 and s.filters.brands == ['라코스테']
    assert not any(k == 'cards' for k, _ in events)
    assert 'No suitable product.' not in ''.join(v['delta'] for k, v in events if k == 'text')


@pytest.mark.asyncio
async def test_same_stage_still_has_only_one_repair():
    bad = tool_events('gift_context', {'queries': ['양말'], 'changes': {'brands': ['라코스톄']}})
    h, s, search, events, run = browse([bad, bad])
    metrics = await run('라코스테 양말', 'repeated')
    assert metrics.status == 'failed' and metrics.calls == 2 and metrics.stages['tool_repairs'] == 1
    assert not search.calls and not s.filters.brands


@pytest.mark.asyncio
@pytest.mark.parametrize('origin', ['COMPOSED_FROM_NAME_AND_CATEGORY', 'COMPOSED_FROM_SOURCE_ATTRIBUTES'])
async def test_derived_category_summary_is_not_model_or_display_evidence(origin):
    h, s, search, events, run = browse([
        tool_events('gift_context', {'queries': ['양말']}),
        tool_events('show_gifts', {'product_ids': ['11']}),
    ])
    original = search.get_products
    async def lookup(ids, snapshot_id):
        response = await original(ids, snapshot_id)
        for p in response['products']:
            p.update(descriptionOrigin=origin,
                     description='잘못된 자동 분류 설명', category='잘못된 자동 분류')
        return response
    search.get_products = lookup
    assert (await run('양말 추천', 'evidence')).status == 'completed'
    assert '잘못된 자동 분류' not in json.dumps(h.provider.calls[-1]['messages'], ensure_ascii=False)
    assert s.last_search['products'][0]['description'] == ''
    assert s.last_search['products'][0]['category_id']
    card = next(v['products'][0] for k, v in events if k == 'cards')
    assert card['evidence']['field'] == 'name' and card['reason'] == '상품 정보: ' + card['name']
    assert h.catalog.products['11']['description'] == '잘못된 자동 분류 설명'


def test_independent_description_remains_usable():
    h, *_ = browse([])
    row = {'name': '원문 이름', 'description': '판매자 제공 독립 설명', 'description_origin': 'SOURCE'}
    text, evidence = h.explanation(row)
    assert text == '상품 정보: 판매자 제공 독립 설명' and evidence['field'] == 'description'


def test_list_targets_preserve_native_decisions_and_legacy_protocol_rejects_new_tool():
    p = manifest('stable-v4.3')
    row = {'harnessVersion': p['version'], 'harnessSha256': p['sourceSha256'], 'serverRenderedTextTarget': False,
        'completion': [{'role': 'assistant', 'content': None, 'tool_calls': [{'function': {
            'name': 'list_gifts', 'arguments': {'product_ids': ['11', '12', '13', '14'], 'format': 'brief'}}}]}]}
    assert validate_records([row]) == p
    old = manifest('stable-v4.2')
    with pytest.raises(ValueError, match='Unregistered'):
        validate_records([{**row, 'harnessVersion': old['version'], 'harnessSha256': old['sourceSha256']}])
    assert omitted_prose_spans('<|tool_call>call:list_gifts{product_ids:["11","12","13","14"],format:"brief"}<tool_call|><|tool_response>') == ([], [])
    with pytest.raises(ValueError):
        omitted_prose_spans('<|tool_call>call:list_gifts{product_ids:["11"],format:"made-up"}<tool_call|><|tool_response>')
    with pytest.raises(ValidationError): CatalogList(product_ids=[str(i) for i in range(31)])
