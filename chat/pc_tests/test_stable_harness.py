"""Protocol invariants, not model-quality measurements."""
import copy
from types import SimpleNamespace
import pytest

from test_guided_harness import setup, PRODUCT
from app.gift_core.contracts import Filters, Profile
from app.gift_core.provider import tool_events
from app.gift_stable import StableHarness, catalog_explanation
from app.gift_stable import StableV4Harness, RankedContext, bounded_history
from app.gift_protocol import harness_class, manifest


async def execute(scripts, *, profile=None, filters=None, intent='auto', bad_scope=False):
    old, session, search, provider, events, _ = setup(scripts, filters, profile)
    async def lookup(ids, snapshot_id):
        return {'snapshotId': snapshot_id, 'products': [copy.deepcopy(PRODUCT)] if 11 in ids else []}
    search.get_products = lookup
    if bad_scope:
        async def outside(request, snapshot_id):
            return {'snapshotId': snapshot_id, 'hits': [{**PRODUCT, 'productId': 12}]}
        search.search = outside
    harness = StableHarness(old.catalog, old.context, provider, turn_timeout=None)
    async def emit(kind, data): events.append((kind, copy.deepcopy(data)))
    await harness.run(session, SimpleNamespace(message='상품 하나 추천해줘', intent_hint=intent, model='fixture'), 't', emit)
    return harness, session, search, events


@pytest.mark.asyncio
@pytest.mark.parametrize('intent', ['auto','gift'])
async def test_prepared_query_runs_before_selection_even_with_ui_gift_hint(intent):
    h,s,search,events = await execute([
        tool_events('gift_context', {'queries':['양말'],'changes':{'max_price':10000}}),
        tool_events('show_gifts', {'product_ids':['11']})], profile=Profile(candidate_ids=['11'],candidate_scope='only'), intent=intent)
    assert h.metrics['t'].status=='completed' and h.metrics['t'].tool_batches==1
    assert search.calls[0]['query']=='양말' and search.calls[0]['filters']['includeProductIds']==[11]
    card=next(v['products'][0] for k,v in events if k=='cards')
    assert card['reason']=='상품 정보: '+PRODUCT['description']
    assert card['evidence']['serverRendered'] and card['evidence']['quote']==PRODUCT['description']
    assert s.filters.max_price==10000


@pytest.mark.asyncio
async def test_repeated_search_gets_stage_hint_and_cannot_mutate_conditions():
    h,s,search,events=await execute([
        tool_events('gift_context', {'queries':['양말'],'changes':{'max_price':10000}}),
        tool_events('gift_context', {'queries':['다른상품'],'changes':{'max_price':99999}}),
        tool_events('show_gifts', {'product_ids':['11']})])
    assert h.metrics['t'].status=='completed' and len(search.calls)==1
    assert s.filters.max_price==10000
    feedback=next(v['feedback'] for k,v in events if k=='tool_repair')
    assert 'product_ids' in feedback['hint']


@pytest.mark.asyncio
async def test_duplicate_ids_fail_without_committing_sparse_changes():
    h,s,search,events=await execute([
        tool_events('gift_context', {'queries':['양말'],'changes':{'max_price':10000}}),
        tool_events('show_gifts', {'product_ids':['11','11']}),
        tool_events('show_gifts', {'product_ids':['11','11']})],filters=Filters(max_price=20000))
    assert h.metrics['t'].status=='failed' and s.filters.max_price==20000
    assert not any(k=='cards' for k,v in events)


@pytest.mark.asyncio
async def test_prepared_scope_invariant_fails_closed_even_if_search_breaks_contract():
    h,s,_,events=await execute([tool_events('gift_context',{'queries':['양말']})],
        profile=Profile(candidate_ids=['11'],candidate_scope='only'),bad_scope=True)
    assert h.metrics['t'].status=='failed' and not any(k=='cards' for k,v in events)


def test_catalog_explanations_keep_option_qualifications_and_do_not_convert_units():
    item={'name':'머그 2P 세트','description':'A 옵션: 면. B 옵션: 폴리에스터.'}
    reason,evidence=catalog_explanation(item)
    assert reason=='상품 정보: A 옵션: 면. B 옵션: 폴리에스터.'
    item['description']='긴 설명'*300
    reason,evidence=catalog_explanation(item)
    assert reason=='상품 정보: 머그 2P 세트' and evidence['field']=='name'


def test_explicit_versions_and_manifest_are_shared(monkeypatch):
    monkeypatch.setenv('GIFT_HARNESS','stable-v3')
    assert harness_class() is StableHarness
    assert manifest()['version']=='stable-v3' and len(manifest()['sourceSha256'])==64
    assert not manifest()['serverTextIsTrainingTarget']
    with pytest.raises(ValueError):harness_class('typo-v3')


@pytest.mark.asyncio
async def test_successful_native_tool_history_survives_turns_but_failed_calls_do_not():
    old,s,search,p,events,_=setup([
        tool_events('gift_context',{'queries':['양말']}),tool_events('show_gifts',{'product_ids':['11']}),
        tool_events('recall_gift',{'position':1}),
        tool_events('gift_context',{'queries':['양말'],'changes':{'max_price':9999}}),
        tool_events('show_gifts',{'product_ids':['missing']}),tool_events('show_gifts',{'product_ids':['missing']})])
    h=StableV4Harness(old.catalog,old.context,p,turn_timeout=None)
    async def emit(kind,data):pass
    def request(message):return SimpleNamespace(message=message,intent_hint='auto',model='fixture')
    await h.run(s,request('하나 추천'), 'one', emit)
    assert h.metrics['one'].status=='completed'
    assert [m['role'] for m in s.history]==['user','assistant','tool','assistant','tool','assistant']
    for a,b in [(s.history[1],s.history[2]),(s.history[3],s.history[4])]:
        assert a['tool_calls'][0]['id']==b['tool_call_id']
    await h.run(s,request('이름과 가격'), 'two', emit)
    saved=copy.deepcopy(s.history)
    assert h.metrics['two'].status=='completed' and len(search.calls)==1
    await h.run(s,request('상한 변경'), 'three', emit)
    assert h.metrics['three'].status=='failed' and s.history==saved
    assert len({m['tool_call_id'] for m in saved if m['role']=='tool'})==3


def test_history_trimming_keeps_complete_tool_pairs_and_context_decorators():
    history=[]
    for i in range(8):
        history += [{'role':'user','content':str(i)},{'role':'assistant','tool_calls':[{'id':str(i)}]},
                    {'role':'tool','tool_call_id':str(i)},{'role':'assistant','content':'완료'}]
    trimmed=bounded_history(history,4)
    assert len(trimmed)==16 and trimmed[0]['content']=='4'
    old,_,_,p,_,_=setup([])
    context=RankedContext(old.catalog)
    async def unavailable(*args,**kwargs):raise RuntimeError('metadata unavailable')
    context.load=unavailable
    stable=StableV4Harness(old.catalog,context,p)
    assert stable.context is context and stable.context.load is unavailable
