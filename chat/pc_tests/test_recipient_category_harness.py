"""CPU category ownership and real harness paths; not model quality evidence."""
import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from test_guided_harness import PRODUCT
from app.gift_adapter import CatalogAdapter, ContextAdapter
from app.gift_core.contracts import Filters, Profile
from app.gift_core.harness import Session
from app.gift_core.provider import ScriptedProvider, tool_events
from app.gift_protocol import harness_class, manifest
from app.gift_recipient_categories import RecipientCategoryHarness
from training_contract import validate_records


def setup(scripts, scope='open', dislikes=('4',)):
    # Parent 4 expands into two distinct leaf categories.
    products = [{**PRODUCT, 'productId': i, 'categoryId': cat, 'price': 9000}
                for i, cat in [(11, 28), (12, 29), (13, 36)]]
    calls = []
    class Search:
        async def get_products(self, ids, snapshot_id):
            return {'snapshotId': snapshot_id, 'products': [copy.deepcopy(p) for p in products if p['productId'] in ids]}
        async def search(self, request, snapshot_id):
            calls.append(copy.deepcopy(request))
            f = request['filters']
            rows = [p for p in products if p['categoryId'] not in f['excludeCategoryIds']
                    and (not f.get('includeProductIds') or p['productId'] in f['includeProductIds'])]
            return {'snapshotId': snapshot_id, 'hits': copy.deepcopy(rows)}
    cat = CatalogAdapter(Search(), {'snapshotId': 'fixture', 'categories': [
        {'id': i, 'name': str(i), 'group': str(parent), 'parentId': parent}
        for i, parent in [(28, 4), (29, 4), (36, 5)]]}, {})
    profile = Profile(disliked_category_ids=list(dislikes), candidate_ids=[] if scope == 'open' else ['11','12'] if scope == 'preferred' else ['11','12','13'],
                      candidate_scope='preferred' if scope == 'preferred' else 'only')
    s = Session('fixture', profile, Filters())
    h = RecipientCategoryHarness(cat, ContextAdapter(cat), ScriptedProvider(scripts), turn_timeout=None)
    events = []
    async def run(tid):
        async def emit(kind, data): events.append((kind, copy.deepcopy(data)))
        await h.run(s, SimpleNamespace(message='같은 조건으로 찾아줘', intent_hint='auto', model='fixture'), tid, emit)
        return h.metrics[tid]
    return h, s, calls, events, run


def query(changes=None): return tool_events('gift_context', {'queries':['선물'], 'changes':changes or {}})
def show(ident='13'): return tool_events('show_gifts', {'product_ids':[ident]})


def test_schema_prompt_and_state_have_no_category_controls_or_dictionary():
    h, s, *_ = setup([])
    fields = h.context_tool(s)['function']['parameters']['properties']['changes']['properties']
    assert set(fields) == {'min_price','max_price','brands','exclude_brands','exclude_product_ids'}
    state = h.state(s, SimpleNamespace(intent_hint='auto'), None)
    assert 'categories' not in state
    assert not {'category_ids','exclude_category_ids','recipient_scope','product_type'} & state['filters'].keys()
    assert 'category_ids' not in h.system_prompt(s, None)
    assert '수신자 설정은 유지' in h.system_prompt(s, None)
    assert 'gift_recipient_categories.py' in manifest('stable-v4.9')['sourceHashes']
    assert 'category_ids' in harness_class('stable-v4.8').context_model.model_fields['changes'].annotation.model_fields


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', ['open','only','preferred'])
async def test_parent_dislikes_applied_to_every_search_path_reset_and_cache(scope):
    reset = {'min_price':None,'max_price':None,'brands':[],'exclude_brands':[],'exclude_product_ids':[]}
    h, s, calls, _, run = setup([query(),show(),query(reset),show()], scope)
    assert (await run('first')).status == 'completed'
    before = len(calls)
    assert (await run('reset')).status == 'completed'
    assert len(calls) == before and h.metrics['reset'].cache_hit
    assert before == (2 if scope == 'preferred' else 1)
    assert all(set(c['filters']['excludeCategoryIds']) == {28,29} for c in calls)
    assert set(s.filters.exclude_category_ids) == {'28','29'} and not s.public_exclusions
    assert [p['product_id'] for p in s.last_products] == ['13']
    for msg in s.history:
        if msg['role'] == 'tool':
            assert not {'category_ids','exclude_category_ids'} & json.loads(msg['content']).get('filters', {}).keys()


@pytest.mark.asyncio
async def test_changed_recipient_settings_invalidate_cache_and_removed_dislike_is_released():
    h, s, calls, _, run = setup([query(),show(),query(),show('11'),query(),show()])
    assert (await run('first')).status == 'completed'
    s.profile.disliked_category_ids = ['5']
    assert (await run('changed')).status == 'completed'
    assert not h.metrics['changed'].cache_hit and calls[-1]['filters']['excludeCategoryIds'] == [36]
    assert [p['product_id'] for p in s.last_products] == ['11']
    s.profile.disliked_category_ids = []
    assert (await run('cleared')).status == 'completed'
    assert calls[-1]['filters']['excludeCategoryIds'] == []
    assert s.filters.exclude_category_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize('patch', [{'category_ids':['5']},{'exclude_category_ids':[]},{'recipient_scope':'other'}])
async def test_removed_controls_cannot_override_profile_and_do_not_reach_search(patch):
    h, s, calls, events, run = setup([query(patch),query(patch)])
    assert (await run('bad')).status == 'failed'
    assert not calls and not any(k in ('cards','catalog') for k, _ in events)
    assert s.profile.disliked_category_ids == ['4']


@pytest.mark.asyncio
async def test_legacy_user_category_filters_retired_but_prices_and_private_profile_preserved():
    h, s, calls, _, run = setup([query(),show()])
    s.filters = Filters(min_price=1000, max_price=20000, category_ids=['4'], exclude_category_ids=['5'])
    s.public_exclusions = ['5']
    s.last_search = {'invalid':'old cache'}
    s.filter_sources = {'category_ids':{'value':['4']}, 'min_price':{'value':1000}}
    assert (await run('migrate')).status == 'completed'
    assert calls[-1]['filters']['categoryIds'] == []
    assert set(calls[-1]['filters']['excludeCategoryIds']) == {28,29}
    assert s.filters.min_price == 1000 and s.filters.max_price == 20000
    assert set(s.filter_sources) == {'min_price'}
