"""CPU protocol fixtures, not measurements of model quality."""
import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from test_three_gifts import three_setup
from app.gift_core.provider import tool_events
from app.gift_core.contracts import Filters
from app.gift_protocol import harness_class, manifest
from app.gift_simple import SimpleHarness
from stable_supervision import sparse_target
from training_contract import validate_records


def setup(scripts):
    old, s, search, events, _ = three_setup(scripts)
    h = SimpleHarness(old.catalog, old.context, old.provider, turn_timeout=None)
    async def run(message, tid):
        events.clear()
        async def emit(kind, data): events.append((kind, copy.deepcopy(data)))
        await h.run(s, SimpleNamespace(message=message, intent_hint='auto', model='fixture'), tid, emit)
        return h.metrics[tid]
    return h, s, search, events, run


def test_current_schema_prompt_and_state_omit_retired_controls_but_keep_history_version():
    h, s, _, _, _ = setup([])
    fields = h.context_tool(s)['function']['parameters']['properties']['changes']['properties']
    assert set(fields) == {'min_price', 'max_price', 'brands', 'exclude_brands',
                           'category_ids', 'exclude_category_ids', 'exclude_product_ids'}
    state = h.state(s, SimpleNamespace(intent_hint='auto'), None)
    for name in ('recipient_scope', 'product_type'):
        assert name not in state['filters']
    assert 'recipient_scope' not in h.system_prompt(s, None)
    assert 'delivery_method' not in h.system_prompt(s, None)
    assert '가격·배송 제한' not in h.system_prompt(s, None)
    assert '이때 서버가 이전 조건을 초기화' not in h.system_prompt(s, None)
    legacy = harness_class('stable-v4.6')(h.catalog, h.context, h.provider)
    old = legacy.context_tool(s)['function']['parameters']['properties']['changes']['properties']
    assert {'recipient_scope', 'delivery_method'} <= old.keys()
    assert 'gift_simple.py' in manifest('stable-v4.8')['sourceHashes']


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [{'delivery_method':'Shipping'}, {'delivery_method':None},
                                   {'recipient_scope':'other'}, {'recipient_scope':'current'},
                                   {'product_type':'Voucher'}])
async def test_removed_arguments_are_rejected_before_io_or_state_commit(changes):
    bad = tool_events('gift_context', {'queries':['양말'], 'changes':changes})
    h, s, search, events, run = setup([bad, bad])
    before = s.filters.model_dump()
    assert (await run('양말 찾아줘', 'invalid')).status == 'failed'
    assert not search.calls and s.filters.model_dump() == before
    assert any(k == 'tool_repair' for k, _ in events)
    assert not any(k in ('cards', 'catalog') for k, _ in events)


@pytest.mark.asyncio
async def test_search_cache_inspection_and_sparse_clear_remain_consistent():
    first = tool_events('gift_context', {'queries':['양말'], 'changes':{'min_price':5000}})
    cached = tool_events('gift_context', {'queries':['양말']})
    clear = tool_events('gift_context', {'queries':['양말'], 'changes':{'max_price':None}})
    show = tool_events('show_gifts', {'product_ids':['11','12','13']})
    h, s, search, events, run = setup([first, show, cached, show,
                                      tool_events('inspect_search', {}), clear, show])
    for tid in ('first', 'cached', 'inspect', 'clear'):
        assert (await run('조건에 맞춰 보여줘', tid)).status == 'completed'
        assert s.filters.min_price == 5000
        assert s.filters.max_price == (None if tid == 'clear' else 20000)
        state = h.state(s, SimpleNamespace(intent_hint='auto'), None)
        assert 'recipient_scope' not in state['last_search']['filters']
        for msg in s.history:
            if msg['role'] == 'tool':
                f = json.loads(msg['content']).get('filters', {})
                assert not {'recipient_scope', 'product_type'} & f.keys()
    assert len(search.calls) == 2
    assert h.metrics['cached'].cache_hit
    assert [p['product_id'] for p in s.last_products] == ['11','12','13']


@pytest.mark.asyncio
async def test_old_session_controls_are_retired_without_resetting_other_filters():
    h, s, search, events, run = setup([
        tool_events('gift_context', {'queries':['양말']}),
        tool_events('show_gifts', {'product_ids':['11']})])
    s.filters = Filters(recipient_scope='other', product_type='Voucher',
                        min_price=1000, max_price=7000, brands=['라코스테'])
    s.last_search = {'old': 'cache must be invalidated before use'}
    assert (await run('같은 조건으로 추천해줘', 'migrated')).status == 'completed'
    assert s.filters.recipient_scope == 'current' and s.filters.product_type is None
    assert s.filters.min_price == 1000 and s.filters.max_price == 7000
    assert s.filters.brands == ['라코스테']
    assert s.last_products[0]['product_type'] == 'Shipping'
    assert len(search.calls) == 1


@pytest.mark.parametrize('removed', [{'recipient_scope':'other'}, {'delivery_method':'Pickup'}])
def test_new_training_contract_rejects_retired_targets(removed):
    protocol = manifest('stable-v4.8')
    row = {'harnessVersion':protocol['version'], 'harnessSha256':protocol['sourceSha256'],
           'serverRenderedTextTarget':False, 'completion':[{'role':'assistant', 'content':None,
           'tool_calls':[{'function':{'name':'gift_context', 'arguments':{'queries':['양말'], 'changes':{}}}}]}]}
    assert validate_records([row]) == protocol
    row['completion'][0]['tool_calls'][0]['function']['arguments']['changes'] = removed
    with pytest.raises(ValidationError): validate_records([row])


def test_supervision_uses_live_schema_and_refuses_incompatible_legacy_scenarios():
    h, s, _, _, _ = setup([])
    schema = h.context_tool(s)['function']['parameters']
    state = h.state(s, SimpleNamespace(intent_hint='auto'), None)
    turn = {'context_args':{'queries':['양말'], 'filters':Filters(max_price=10000, budget_action='replace').model_dump()}}
    assert sparse_target(turn, state, schema=schema) == {'queries':['양말'], 'changes':{'max_price':10000}}
    for patch in ({'recipient_scope':'other'}, {'product_type':'Voucher'}):
        turn['context_args']['filters'] = Filters(**patch).model_dump()
        with pytest.raises(ValueError, match='Scenario requires'):
            sparse_target(turn, state, schema=schema)
