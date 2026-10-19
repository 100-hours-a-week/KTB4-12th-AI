"""Provider wire and execution invariants; no GPU or model-quality claims."""
import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.gift_clean_io import CleanIOHarness, Listing
from app.gift_core.provider import tool_events
from app.gift_protocol import manifest, DEFAULT
from test_compact_harness import Recorded, run
from test_three_gifts import three_setup
from test_recipient_category_harness import setup as category_setup, query, show
from training_contract import validate_records


def setup(scripts):
    old, s, search, _, _ = three_setup([])
    provider = Recorded(scripts)
    return CleanIOHarness(old.catalog, old.context, provider, turn_timeout=None), s, search, provider


def test_small_output_schema_keeps_strict_types_and_limits():
    h, s, _, _ = setup([])
    for tool, count in [(h.selection_tool(s, ['11','12']), 3), (h.list_tool(['11','12']), 30)]:
        schema = tool['function']['parameters']
        assert schema['required'] == ['product_ids']
        assert schema['additionalProperties'] is False
        assert schema['properties']['product_ids']['maxItems'] == count
        assert 'enum' not in schema['properties']['product_ids']['items']
        assert 'one_per_brand' not in schema['properties']
    assert h.inspect_tool()['function']['parameters'] == {'additionalProperties':False,'properties':{},'type':'object'}
    for invalid in ({'product_ids':'["11"]'}, {'product_ids':['11'],'one_per_brand':True}):
        with pytest.raises(ValidationError):
            Listing.model_validate(invalid)


@pytest.mark.asyncio
async def test_recommend_redisplay_recall_has_no_research_or_boilerplate():
    h, s, search, provider = setup([query(), show('11'),
        tool_events('list_gifts', {'product_ids':['11'], 'format':'names'}),
        tool_events('list_gifts', {'product_ids':['11'], 'format':'brief'}),
        tool_events('recall_gift', {'position':1})])
    cards = await run(h, s, '양말 하나 추천해줘', 'cards')
    assert [(k,v['delta']) for k,v in cards if k=='text'] == []
    names = await run(h, s, '방금 그 상품 이름만', 'names')
    name_text = next(v['delta'] for k,v in names if k=='text')
    assert name_text == '1. 면 혼방 양말 상품 11'
    gallery = await run(h, s, '사진도 보여줘', 'gallery')
    assert [p['product_id'] for k,v in gallery if k=='catalog' for p in v['products']] == ['11']
    assert [v['delta'] for k,v in gallery if k=='text'] == []
    recall = await run(h, s, '첫 번째 얼마야?', 'recall')
    assert any('5,500원' in v['delta'] for k,v in recall if k=='text')
    assert len(search.calls) == 1 and len(provider.calls) == 5
    assert all(not h.metrics[t].stages.get('tool_repairs') for t in ('cards','names','gallery','recall'))
    assert 'show_gifts' not in {t['function']['name'] for t in provider.calls[2]['tools']}
    assert 'list_gifts' in {t['function']['name'] for t in provider.calls[2]['tools']}
    history = provider.calls[-1]['messages']
    # The names-only answer occurs once, not again as a tool-result text field.
    assert sum(name_text == m.get('content') for m in history) == 1
    assert not any('serverRendered' in m.get('content','') for m in history if m['role']=='tool')


@pytest.mark.asyncio
@pytest.mark.parametrize('ids,mode', [(['unknown'],'searched'), (['11','11'],'searched'), (['12'],'redisplay')])
async def test_unobserved_duplicate_or_new_redisplay_ids_do_not_commit(ids, mode):
    bad = tool_events('list_gifts', {'product_ids':ids})
    scripts = [query(),show('11')]
    if mode == 'searched':
        scripts += [query()]
    scripts += [bad,bad]
    h, s, _, _ = setup(scripts)
    await run(h, s, '하나 골라줘', 'first')
    before = copy.deepcopy((s.last_search,s.last_products,s.history))
    events = []
    async def emit(k,v): events.append((k,v))
    await h.run(s, SimpleNamespace(message='목록 보여줘', intent_hint='auto', model='fixture'), 'bad', emit)
    assert h.metrics['bad'].status == 'failed'
    assert (s.last_search,s.last_products) == before[:2]
    assert s.history[:len(before[2])] == before[2]
    assert not any(m['role']=='tool' for m in s.history[len(before[2]):])
    assert not any(k in ('cards','catalog') for k,v in events)


@pytest.mark.asyncio
async def test_changed_filters_go_through_search_and_brand_choices_are_not_rewritten():
    h, s, search, provider = setup([query(), show('11'),
        tool_events('gift_context', {'queries':['양말'],'changes':{'max_price':10000}}),
        tool_events('list_gifts', {'product_ids':['12','11'], 'format':'brief', 'message':'선택 이유만 안내합니다.'})])
    await run(h, s, '하나 추천', 'first')
    events = await run(h, s, '만원 이하 두 개', 'second')
    assert s.filters.max_price == 10000 and len(search.calls) == 2
    products = [p for k,v in events if k=='catalog' for p in v['products']]
    assert [p['product_id'] for p in products] == ['12','11']
    assert len({p['brand'] for p in products}) == 1  # No automatic brand dedup.
    assert [v['delta'] for k,v in events if k=='text'] == ['선택 이유만 안내합니다.']


@pytest.mark.asyncio
async def test_history_keeps_old_display_facts_and_actual_native_calls_without_mutation():
    h, s, _, _ = setup([query(),tool_events('list_gifts', {'product_ids':['14'],'format':'brief'}),
                        query(),show('11')])
    await run(h, s, '사진 목록 하나', 'old')
    await run(h, s, '다른 추천 하나', 'new')
    saved = copy.deepcopy(s.history)
    projected = h.prompt_history(s)
    assert s.history == saved
    assert [m for m in projected if m['role'] != 'tool'] == [m for m in saved if m['role'] != 'tool']
    results = [json.loads(m['content']) for m in projected if m['role']=='tool']
    assert any(p['product_id']=='14' and p['name']=='면 혼방 양말 상품 14' and p['price']==7000
               for r in results for p in r.get('products',[]))
    assert not any(p['product_id']=='11' for r in results for p in r.get('products',[]))  # In STATE_JSON.
    calls = {c['id'] for m in projected for c in m.get('tool_calls',[])}
    assert all(m['tool_call_id'] in calls for m in projected if m['role']=='tool')


@pytest.mark.asyncio
@pytest.mark.parametrize('scope',['open','only','preferred'])
async def test_private_category_exclusions_survive_the_new_wire_format(scope):
    old,s,calls,_,_ = category_setup([query(),show()],scope)
    h = CleanIOHarness(old.catalog,old.context,Recorded(old.provider.scripts),turn_timeout=None)
    await run(h,s,'선물 하나','privacy')
    assert all(set(c['filters']['excludeCategoryIds']) == {28,29} for c in calls)
    wire = json.loads(h.provider.calls[-1]['messages'][-1]['content'])
    assert 'exclude_category_ids' not in wire['filters']
    assert all(set(p) <= {'product_id','name','brand','price','product_type','description'} for p in wire['products'])


@pytest.mark.asyncio
async def test_training_uses_the_new_actual_prompts_and_schema():
    h,s,_,provider = setup([query(),show('11')])
    await run(h,s,'선물 하나','training')
    protocol = manifest(h.variant)
    assert h.variant == 'stable-v4.11'
    assert 'gift_clean_io.py' in protocol['sourceHashes']
    assert 'gift_clean_io.py' not in manifest('stable-v4.10')['sourceHashes']
    rows = []
    for call,name,args in [(provider.calls[0],'gift_context',{'queries':['선물']}),
                           (provider.calls[1],'show_gifts',{'product_ids':['11']})]:
        rows.append({'harnessVersion':h.variant,'harnessSha256':protocol['sourceSha256'],
                     'serverRenderedTextTarget':False,'prompt':call['messages'],'tools':call['tools'],
                     'completion':[{'role':'assistant','content':None,'tool_calls':[{'function':{'name':name,'arguments':args}}]}]})
    assert validate_records(rows) == protocol
