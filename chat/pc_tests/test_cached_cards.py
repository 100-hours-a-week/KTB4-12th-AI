"""Synthetic reproductions of list -> cards, not model quality measurements."""
import copy
import json
from types import SimpleNamespace

import pytest

from test_three_gifts import three_setup
from app.gift_cached import CachedCardHarness
from app.gift_browse import BrowseHarness
from app.gift_core.provider import tool_events


def setup(scripts):
    old, s, search, events, _ = three_setup(scripts)
    h = CachedCardHarness(old.catalog, old.context, old.provider, turn_timeout=None)
    async def run(message, tid):
        events.clear()
        async def emit(kind, data): events.append((kind, copy.deepcopy(data)))
        await h.run(s, SimpleNamespace(message=message, intent_hint='auto', model='fixture'), tid, emit)
        return h.metrics[tid]
    return h, s, search, events, run


FIRST = [tool_events('gift_context', {'queries': ['양말'], 'changes': {'max_price': 10000}}),
         tool_events('list_gifts', {'product_ids': ['11', '12', '13', '14'], 'format': 'names'})]


@pytest.mark.asyncio
async def test_list_to_two_cards_after_reload_rehydrates_selected_ids_without_search():
    h, s, search, events, run = setup(FIRST + [tool_events('show_gifts', {'product_ids': ['14', '12']})])
    assert (await run('상품명 목록', 'list')).status == 'completed'
    assert not s.last_products
    legacy = BrowseHarness(h.catalog, h.context, h.provider)
    assert 'show_gifts' not in [t['function']['name'] for t in legacy.answer_tools(s)]
    s.last_search = json.loads(json.dumps(s.last_search))
    h.catalog.products.clear()  # New web-turn adapter has no unselected rows.
    lookups=[];original=search.get_products
    async def lookup(ids, snapshot_id):
        lookups.append((ids, snapshot_id));return await original(ids, snapshot_id)
    search.get_products=lookup
    m=await run('두 개만 추천', 'cards')
    assert m.status == 'completed' and m.calls == 1 and len(search.calls) == 1
    assert lookups == [([14, 12], 'test')]
    cards=next(v['products'] for k,v in events if k=='cards')
    assert [p['product_id'] for p in cards] == ['14', '12']
    assert s.filters.max_price == 10000 and len(s.last_search['products']) == 4
    assert h.catalog.receipt['selectionSource']['source'] == 'last_search'
    offered={t['function']['name']:t for t in h.provider.calls[-1]['tools']}
    assert set(offered)=={'gift_context','list_gifts','show_gifts'}
    assert offered['show_gifts']['function']['parameters']['properties']['product_ids']['items']['enum']==['11','12','13','14']


@pytest.mark.asyncio
async def test_stringified_id_array_gets_typed_repair_then_cards():
    h,s,search,events,run=setup(FIRST+[
        tool_events('show_gifts', {'product_ids': '["14", "12"]'}),
        tool_events('show_gifts', {'product_ids': ['14','12']}),
    ])
    await run('목록', 'list')
    m=await run('두 개 추천', 'repair')
    assert m.status=='completed' and m.calls==2 and len(search.calls)==1
    feedback=next(v['feedback'] for k,v in events if k=='tool_repair')
    assert feedback['fields']==[{'location':'product_ids','type':'list_type'}]
    assert 'JSON 배열' in feedback['hint']
    assert [p['product_id'] for p in s.last_products]==['14','12']


@pytest.mark.asyncio
async def test_failed_call_cannot_fall_through_to_ungrounded_plain_recommendations():
    h,s,search,events,run=setup(FIRST+[
        tool_events('show_gifts', {'product_ids': '["14", "12"]'}),
        [{'kind':'text','value':'The tool is unavailable; this item has invented features.'},{'kind':'finish','value':'stop'}],
    ])
    await run('목록', 'list');prior=copy.deepcopy(s.last_search)
    m=await run('두 개 추천', 'bad')
    assert m.status=='failed' and m.calls==2 and not s.last_products
    assert s.last_search==prior and not any(k in ('text','cards') for k,_ in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['snapshot','recipient','filters'])
async def test_stale_cache_does_not_offer_card_selection(change):
    h,s,_,_,run=setup(FIRST)
    await run('목록', 'list')
    assert 'show_gifts' in [t['function']['name'] for t in h.answer_tools(s)]
    if change=='snapshot':h.catalog.snapshot='changed'
    elif change=='recipient':s.profile=s.profile.model_copy(update={'recipient_id':'other'})
    else:s.filters=s.filters.model_copy(update={'max_price':1})
    assert h.cached_search(s) is None
    assert 'show_gifts' not in [t['function']['name'] for t in h.answer_tools(s)]


@pytest.mark.asyncio
async def test_missing_rehydrated_product_cannot_use_stale_adapter_row():
    pick=tool_events('show_gifts',{'product_ids':['14']})
    h,s,search,events,run=setup(FIRST+[pick,pick])
    await run('목록','list');assert '14' in h.catalog.products
    async def missing(ids,snapshot_id):return {'snapshotId':snapshot_id,'products':[]}
    search.get_products=missing
    assert (await run('하나 추천','missing')).status=='failed'
    assert not any(k=='cards' for k,_ in events) and len(search.calls)==1


@pytest.mark.asyncio
async def test_fresh_context_supersedes_cached_candidates_and_filters():
    bad=tool_events('show_gifts',{'product_ids':['14']})
    h,s,search,events,run=setup(FIRST+[
        tool_events('gift_context',{'queries':['양말'],'changes':{'max_price':9000}}),bad,bad])
    await run('목록','list');prior=copy.deepcopy(s.last_search)
    original=search.search
    async def subset(request,snapshot_id):
        data=await original(request,snapshot_id);data['hits']=data['hits'][:1];return data
    search.search=subset
    assert (await run('9천원 이하로 다시 추천','new')).status=='failed'
    assert len(search.calls)==2 and s.filters.max_price==10000 and s.last_search==prior
    assert not any(k=='cards' for k,_ in events)
