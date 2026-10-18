"""State integrity and catalog grounding; fixtures are not model-quality scores."""
import copy
from types import SimpleNamespace

import pytest

from app.gift_adapter import CatalogAdapter, ContextAdapter, view
from app.gift_core.contracts import Filters, Profile, ClarificationNeeded
from app.gift_core.harness import Session
from app.gift_core.provider import ScriptedProvider, tool_events
from app.gift_guided import GuidedHarness, BrandResolver


PRODUCT = {'productId': 11, 'categoryId': 1, 'name': '라코스테 3팩 양말', 'price': 9000,
           'description': '면 혼방 소재 양말', 'brand': '라코스테', 'category': '양말',
           'productType': 'Shipping', 'availability': 'UNKNOWN'}


class Search:
    def __init__(self):
        self.calls = []

    async def get_products(self, ids, snapshot_id):
        return {'snapshotId': snapshot_id, 'products': [PRODUCT]}

    async def search(self, request, snapshot_id):
        self.calls.append(request)
        return {'snapshotId': snapshot_id, 'hits': [PRODUCT]}


def setup(scripts, filters=None, profile=None):
    search = Search()
    catalog = CatalogAdapter(search, {'snapshotId': 'test', 'categories': [
        {'id': 1, 'name': '양말', 'group': '의류', 'parentId': 1}],
        'brands': [{'name': '라코스테'}, {'name': '폴로 랄프 로렌(의류)'}]}, {})
    provider = ScriptedProvider(scripts)
    harness = GuidedHarness(catalog, ContextAdapter(catalog), provider, turn_timeout=None)
    session = Session('test', profile or Profile(), filters or Filters())
    events = []
    async def run(message='예산만 바꿔줘', tid='turn'):
        async def emit(kind, data):
            events.append((kind, copy.deepcopy(data)))
        await harness.run(session, SimpleNamespace(message=message, intent_hint='auto', model='fixture'), tid, emit)
        return harness.metrics[tid]
    return harness, session, search, provider, events, run


def select(reason='면 혼방 소재로 만든 양말입니다.', pid='11'):
    return tool_events('show_gifts', {'message': '조건에 맞는 양말이에요.', 'products': [{'product_id': pid, 'reason': reason}]})


@pytest.mark.asyncio
async def test_sparse_budget_update_preserves_brand_exclusion_and_commits_provenance():
    filters = Filters(brands=['라코스테'], exclude_brands=['폴로 랄프 로렌(의류)'], min_price=1000, max_price=20000)
    h, s, search, p, events, run = setup([
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'max_price': 10000}}), select()], filters)
    metrics = await run('가격 상한만 만 원으로 바꿔줘')
    assert metrics.status == 'completed' and metrics.calls == 2
    assert s.filters.brands == filters.brands and s.filters.exclude_brands == filters.exclude_brands
    assert s.filters.min_price == 1000 and s.filters.max_price == 10000
    assert set(s.filter_sources) == {'max_price'}
    assert s.filter_sources['max_price']['message'] == '가격 상한만 만 원으로 바꿔줘'
    assert search.calls[0]['filters']['maxPrice'] == 10000


@pytest.mark.asyncio
async def test_explicit_clear_and_alias_resolution_are_applied_before_search():
    h, s, search, p, events, run = setup([
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'max_price': None, 'brands': [], 'exclude_brands': ['폴로 랄프 로렌']}}),
        select('면 혼방 소재의 3팩 구성입니다.')], Filters(brands=['라코스테'], min_price=5000, max_price=10000))
    assert (await run()).status == 'completed'
    assert s.filters.max_price is None and s.filters.min_price == 5000
    assert s.filters.brands == [] and s.filters.exclude_brands == ['폴로 랄프 로렌(의류)']
    assert search.calls[0]['filters']['excludeBrands'] == ['폴로 랄프 로렌(의류)']
    assert h.metrics['turn'].calls == 2  # Source-backed number needs no repair.


@pytest.mark.asyncio
async def test_failed_selection_does_not_commit_filters_or_provenance():
    h, s, search, p, events, run = setup([
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'max_price': 10000}}),
        select(pid='not-observed'), select(pid='not-observed')], Filters(max_price=20000))
    s.filter_sources = {'max_price': {'value': 20000, 'message': '원래 조건'}}
    assert (await run()).status == 'failed'
    assert s.filters.max_price == 20000 and s.filter_sources['max_price']['value'] == 20000
    assert not any(kind == 'cards' for kind, _ in events)


@pytest.mark.asyncio
async def test_contradictory_range_does_not_search_or_commit():
    h, s, search, p, events, run = setup([
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'max_price': 1000}})], Filters(min_price=5000, max_price=20000))
    metrics = await run()
    assert metrics.status == 'completed' and not search.calls
    assert s.filters.max_price == 20000 and not hasattr(s, 'filter_sources')
    assert any(kind == 'text' and '어느 조건' in data['delta'] for kind, data in events)


@pytest.mark.asyncio
async def test_unknown_patch_key_is_rejected_instead_of_silently_clearing_state():
    h, s, search, p, events, run = setup([
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'brand': []}}),
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'brand': []}})], Filters(brands=['라코스테']))
    assert (await run()).status == 'failed'
    assert not search.calls and s.filters.brands == ['라코스테']


@pytest.mark.asyncio
async def test_recall_renders_authoritative_name_and_price_without_search():
    h, s, search, p, events, run = setup([tool_events('recall_gift', {'position': 2})])
    s.last_products = [view({**PRODUCT, 'productId': 12, 'name': '첫 번째 상품'}), view(PRODUCT)]
    assert (await run('둘째 상품 이름과 가격 알려줘')).status == 'completed'
    text = ''.join(v['delta'] for k, v in events if k == 'text')
    assert '라코스테 3팩 양말' in text and '9,000원' in text and not search.calls
    assert not any(k == 'cards' for k, v in events)
    operation = h.catalog.receipt['diagnostics']['operations'][-1]
    assert operation['name'] == 'recall_gift' and operation['status'] == 'completed'
    assert operation['result']['text'] == text and operation['elapsedMs'] >= 0


@pytest.mark.asyncio
async def test_empty_retrieval_commits_filter_without_another_model_decision():
    h, s, search, p, events, run = setup([tool_events('gift_context', {'queries': ['양말'], 'changes': {'max_price': 0}})])
    async def no_hits(request, snapshot_id):
        search.calls.append(request)
        return {'snapshotId': snapshot_id, 'hits': []}
    search.search = no_hits
    metrics = await run('예산 상한을 0원으로 바꿔줘')
    assert metrics.status == 'completed' and metrics.calls == 1 and len(search.calls) == 1
    assert s.filters.max_price == 0 and s.filter_sources['max_price']['value'] == 0
    assert not any(k == 'cards' for k, v in events)
    assert h.catalog.receipt['diagnostics']['operations'][-1]['name'] == 'empty_results'


@pytest.mark.asyncio
@pytest.mark.parametrize('composition, expected_repairs', [('구성: 머그2P. 소재: 도자기.', 0), ('구성: 머그 13P.', 1), ('구성: 모델X2P.', 1), ('규격: 2P.', 1)])
async def test_piece_count_equivalence_requires_explicit_composition(composition, expected_repairs):
    h, s, search, p, events, run = setup([
        tool_events('gift_context', {'queries': ['머그컵']}), select('머그 2개 구성입니다.'), select('도자기 머그입니다.')])
    async def mugs(request, snapshot_id):
        return {'snapshotId': snapshot_id, 'hits': [{**PRODUCT, 'name': '도자기 머그', 'description': composition}]}
    search.search = mugs
    metrics = await run('머그컵을 골라줘')
    assert metrics.status == 'completed'
    assert metrics.stages.get('tool_repairs', 0) == expected_repairs


@pytest.mark.asyncio
async def test_recipient_switch_resets_old_constraints():
    h, s, search, p, events, run = setup([
        tool_events('gift_context', {'queries': ['양말'], 'changes': {'recipient_scope': 'other', 'max_price': 15000}}), select()],
        Filters(brands=['없는브랜드'], exclude_brands=['라코스테'], max_price=1000))
    assert (await run()).status == 'completed'
    assert s.filters.brands == [] and s.filters.exclude_brands == []
    assert s.filters.recipient_scope == 'other' and s.filters.max_price == 15000


def test_brand_resolution_is_unique_and_not_fuzzy():
    b = BrandResolver(['브랜드(의류)', '브랜드(식품)', '폴로 랄프 로렌(의류)'])
    assert b.resolve('폴로랄프로렌') == '폴로 랄프 로렌(의류)'
    assert b.resolve('폴로 랄프 로렌(의류)') == '폴로 랄프 로렌(의류)'
    assert b.resolve('폴로 랄프 로랜') == '폴로 랄프 로랜'
    with pytest.raises(ClarificationNeeded):
        b.resolve('브랜드')


@pytest.mark.asyncio
async def test_unverified_numeric_claim_still_requires_correction():
    h, s, search, p, events, run = setup([
        tool_events('gift_context', {'queries': ['양말']}), select('30팩 구성입니다.'), select('면 혼방 소재로 만든 양말입니다.')])
    metrics = await run()
    assert metrics.status == 'completed' and metrics.stages['tool_repairs'] == 1
    assert all('30팩' not in str(v) for k, v in events if k in ('cards', 'text'))


@pytest.mark.asyncio
async def test_price_formatting_and_sentence_punctuation_are_not_numeric_fabrication():
    h, s, search, p, events, run = setup([
        tool_events('gift_context', {'queries': ['양말']}), select('3팩 구성이고 표시 가격은 9,000원입니다.')])
    assert (await run()).status == 'completed'
    assert h.metrics['turn'].calls == 2


@pytest.mark.asyncio
async def test_search_followed_by_plain_prose_is_corrected_before_text_is_committed():
    plain=[{'kind':'text','value':'상품을 골랐어요.'},{'kind':'finish','value':'stop'}]
    h,s,search,p,events,run=setup([tool_events('gift_context',{'queries':['양말']}),plain,select()])
    metrics=await run()
    assert metrics.status=='completed' and metrics.calls==3
    assert not any(k=='text' and v['delta']=='상품을 골랐어요.' for k,v in events)
    assert sum(k=='cards' for k,v in events)==1


@pytest.mark.asyncio
async def test_literal_measurement_before_sentence_period_is_accepted(monkeypatch):
    monkeypatch.setitem(PRODUCT,'description','길이 30cm. 소재 비율 4%.')
    h,s,search,p,events,run=setup([tool_events('gift_context',{'queries':['양말']}),select('길이는 30cm이고 소재 비율은 4%입니다.')])
    assert (await run()).status=='completed'
    assert h.metrics['turn'].calls==2
