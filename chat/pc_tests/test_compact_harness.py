"""Input projection and unchanged real harness execution; CPU fixtures only."""
import copy
import json
from types import SimpleNamespace

import pytest

from test_three_gifts import three_setup
from test_recipient_category_harness import setup as category_setup, query, show
from app.gift_compact import CompactHarness, product_input, filter_input
from app.gift_recipient_categories import RecipientCategoryHarness
from app.gift_core.provider import ScriptedProvider, tool_events
from app.gift_protocol import manifest, harness_class, DEFAULT
from training_contract import validate_records


class Recorded(ScriptedProvider):
    async def stream(self, messages, tools, model):
        async for event in super().stream(copy.deepcopy(messages), copy.deepcopy(tools), model):
            yield event


def test_product_projection_preserves_decisive_facts_without_metadata_or_fabrication():
    description = '본체 제외. C타입 기기 전용이며 케이블은 별도 구매입니다. 옵션 A: 1개, 옵션 B: 2개.'
    row = dict(product_id='1', name='교체용 부품', brand='브랜드', price=0, product_type='Shipping',
               description=description, description_origin='backend-export', sold_out=False,
               availability='UNKNOWN', category='기기', category_id='24', category_status='ASSIGNED',
               image='private-image', future_internal_metadata='never_send')
    before = copy.deepcopy(row)
    projected = product_input(row)
    assert projected == {key:row[key] for key in ('product_id','name','brand','price','product_type','description')}
    assert row == before
    assert 'description' not in product_input({**row, 'description_origin':'COMPOSED_FROM_NAME_AND_CATEGORY'})
    assert 'description' not in product_input({**row, 'description':row['name']})
    assert 'description' not in product_input({**row, 'description':'  '})
    assert filter_input({'min_price':0, 'max_price':None, 'brands':[], 'exclude_brands':['A'],
                         'category_ids':['24'], 'budget_action':'clear'}) == {'min_price':0, 'exclude_brands':['A']}


async def run(h, s, text, tid):
    events = []
    async def emit(kind, data): events.append((kind, copy.deepcopy(data)))
    await h.run(s, SimpleNamespace(message=text, intent_hint='auto', model='fixture'), tid, emit)
    assert h.metrics[tid].status == 'completed', h.metrics[tid].report()
    return events


@pytest.mark.asyncio
async def test_same_actions_keep_cards_prices_order_cache_and_full_saved_evidence():
    q = tool_events('gift_context', {'queries':['양말'], 'changes':{'max_price':10000}})
    scripts = [q, tool_events('show_gifts', {'product_ids':['13','11','12']}),
               tool_events('gift_context', {'queries':['양말']}),
               tool_events('list_gifts', {'product_ids':['14','12'], 'format':'brief'}),
               tool_events('recall_gift', {'position':1}),
               tool_events('gift_context', {'queries':['양말'], 'changes':{'max_price':None}}),
               tool_events('show_gifts', {'product_ids':['11']})]
    results = []
    for cls in (RecipientCategoryHarness, CompactHarness):
        old, s, search, _, _ = three_setup([])
        provider = Recorded(copy.deepcopy(scripts))
        h = cls(old.catalog, old.context, provider, turn_timeout=None)
        outputs = []
        for i, text in enumerate(['만원 이하 세 개 추천', '다른 두 개 사진 보여줘', '첫 번째 얼마야?', '상한은 없애줘']):
            events = await run(h, s, text, str(i))
            outputs.append([(k,v) for k,v in events if k in ('text','cards','catalog')])
        assert len(search.calls) == 2  # Same-condition display reused the search.
        assert s.filters.max_price is None
        assert s.last_products[0]['product_id'] == '11'
        assert 'category_id' in s.last_search['products'][0]
        assert 'category_status' in h.catalog.products['11']
        results.append((outputs, s.filters.model_dump(), s.last_products, s.last_search))
        if cls is CompactHarness:
            wire = json.loads(provider.calls[1]['messages'][-1]['content'])
            assert set(wire) == {'queries','filters','products','retrieved_count','candidate_price_order_desc'}
            assert wire['candidate_price_order_desc'] == ['14','13','12','11']
            assert wire['retrieved_count'] == 4
            assert all('category_id' not in row and 'sold_out' not in row for row in wire['products'])
            state = json.loads(provider.calls[2]['messages'][0]['content'].split('STATE_JSON\n')[1])
            assert 'products' not in state['last_search'] and 'last_displayed_products' not in state
            recall_state = json.loads(provider.calls[4]['messages'][0]['content'].split('STATE_JSON\n')[1])
            assert [p['product_id'] for p in recall_state['last_displayed_products']] == ['14','12']
            assert [p['position'] for p in recall_state['last_displayed_products']] == [1,2]
            assert 'source' not in json.loads(next(m['content'] for m in provider.calls[2]['messages'] if m['role']=='tool'))
            # Every preserved native history tool result still has its call.
            for call in provider.calls:
                ids = {c['id'] for m in call['messages'] for c in m.get('tool_calls', [])}
                assert all(m['tool_call_id'] in ids for m in call['messages'] if m['role']=='tool')
    # No semantic selection, rendering, filtering, or cache behavior changed.
    assert results[0] == results[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', ['open','only','preferred'])
async def test_projection_does_not_remove_private_filters_from_any_search_path(scope):
    old, s, calls, _, _ = category_setup([query(),show()], scope)
    provider = Recorded(old.provider.scripts)
    h = CompactHarness(old.catalog, old.context, provider, turn_timeout=None)
    await run(h, s, '선물 하나 추천해줘', 'fixture')
    assert all(set(c['filters']['excludeCategoryIds']) == {28,29} for c in calls)
    assert [p['product_id'] for p in s.last_products] == ['13']
    assert s.profile.disliked_category_ids == ['4']
    wire = json.loads(provider.calls[-1]['messages'][-1]['content'])
    assert 'exclude_category_ids' not in wire['filters']
    assert wire['products'][0]['product_id'] == '13'


@pytest.mark.asyncio
async def test_training_accepts_actual_new_inputs_and_does_not_relabel_old_data():
    old, s, _, _, _ = three_setup([])
    provider = Recorded([query(),show('11')])
    h = CompactHarness(old.catalog, old.context, provider, turn_timeout=None)
    await run(h, s, '선물 하나 추천해줘', 'fixture')
    protocol = manifest(CompactHarness.variant)
    assert protocol['version'] == 'stable-v4.10'
    assert 'gift_compact.py' in protocol['sourceHashes']
    assert 'gift_compact.py' not in manifest('stable-v4.9')['sourceHashes']
    rows = []
    for call, name, args in [(provider.calls[0], 'gift_context', {'queries':['선물'],'changes':{}}),
                             (provider.calls[1], 'show_gifts', {'product_ids':['11']})]:
        rows.append({'harnessVersion':CompactHarness.variant, 'harnessSha256':protocol['sourceSha256'],
            'serverRenderedTextTarget':False, 'prompt':call['messages'], 'tools':call['tools'],
            'completion':[{'role':'assistant','content':None,'tool_calls':[{'function':{'name':name,'arguments':args}}]}]})
    assert validate_records(rows) == protocol
    with pytest.raises(ValueError, match='source mismatch'):
        validate_records([{**rows[0], 'harnessSha256':manifest('stable-v4.9')['sourceSha256']}])
    original = {'products':[{'product_id':'1','category_id':'24'}], 'snapshot':'old'}
    legacy = harness_class('stable-v4.9').__new__(harness_class('stable-v4.9'))
    assert legacy.model_context(original) is original


def test_projection_is_idempotent_and_keeps_historical_summary_counts():
    h = CompactHarness.__new__(CompactHarness)
    history = {'source':'catalog_search', 'snapshot':'hash', 'queries':['커피'], 'filters':{'max_price':30000},
               'summary':True,'status':'completed','retrieved_count':15}
    projected = h.model_context(history)
    assert projected['retrieved_count'] == 15 and 'products' not in projected
    assert h.model_context(projected) == projected
