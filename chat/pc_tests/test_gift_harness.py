"""Protocol/transaction tests. Synthetic calls do not measure model quality."""
import copy
import json
from types import SimpleNamespace

import pytest

from app.gift_adapter import CatalogAdapter, ContextAdapter, PublicHarness
from app.gift_core.contracts import Filters, Profile
from app.gift_core.harness import Session
from app.gift_core.provider import ScriptedProvider, ProviderError, tool_events
from app.conversation_memory import history_messages


class Search:
    def __init__(self): self.calls = []
    async def search(self, request, snapshot_id):
        self.calls.append(copy.deepcopy(request))
        return {'snapshotId': snapshot_id, 'hits': [{
            'productId': 11, 'categoryId': 1, 'name': '휴대용 텀블러',
            'price': 20000, 'description': '휴대용 음료 용기', 'brand': '브랜드',
            'category': '주방', 'productType': 'Shipping', 'availability': 'UNKNOWN'}]}
    async def get_products(self, ids, snapshot_id):
        return await self.search({},snapshot_id)


class Provider(ScriptedProvider):
    async def stream(self, messages, tools, model):
        async for event in super().stream(copy.deepcopy(messages), copy.deepcopy(tools), model):
            yield event


def setup(scripts):
    search = Search()
    catalog = CatalogAdapter(search, {'snapshotId': 'synthetic', 'categories': [
        {'id': 1, 'name': '주방', 'group': '생활', 'parentId': 1}]}, {})
    provider = Provider(scripts)
    harness = PublicHarness(catalog, ContextAdapter(catalog), provider, turn_timeout=None)
    session = Session('test', Profile(), Filters())
    events = []
    async def emit(kind, value): events.append((kind, copy.deepcopy(value)))
    async def run():
        await harness.run(session, SimpleNamespace(message='텀블러를 추천해줘', model='fixture', intent_hint='auto'), 'turn', emit)
    return run, search, provider, session, events, harness


def context():
    return {'queries': ['텀블러'], 'filters': Filters(budget_action='replace', max_price=30000).model_dump(), 'expand_catalog': False}


def selection(product_id='11', reason='휴대용 음료 용기로 사용할 수 있어요.'):
    return {'message': '휴대용으로 쓸 수 있는 상품이에요.', 'products': [{'product_id': product_id, 'reason': reason}]}


@pytest.mark.asyncio
async def test_invalid_context_can_be_corrected_before_one_search():
    bad = context(); del bad['filters']['brands']
    run, search, provider, session, events, harness = setup([
        tool_events('gift_context', bad), tool_events('gift_context', context()), tool_events('show_gifts', selection())])
    await run()
    assert harness.metrics['turn'].status == 'completed'
    assert len(provider.calls) == 3 and len(search.calls) == 1
    assert search.calls[0]['filters']['maxPrice'] == session.filters.max_price == 30000
    assert provider.calls[0]['messages'][0] == provider.calls[2]['messages'][0]
    feedback = json.loads(provider.calls[1]['messages'][-1]['content'])
    assert feedback['ok'] is False and feedback['error']['code'] == 'incomplete_tool_object'
    tool_result = json.loads(provider.calls[2]['messages'][-1]['content'])
    assert tool_result['filters']['max_price'] == 30000
    assert [p['product_id'] for p in session.last_products] == ['11']
    assert sum(kind == 'cards' for kind, _ in events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [selection('999'), selection(reason='음료를 2개 담아요.'),selection(reason='   ')])
async def test_rejected_selection_is_never_published_and_gets_one_correction(bad):
    run, search, provider, session, events, harness = setup([
        tool_events('gift_context', context()), tool_events('show_gifts', bad), tool_events('show_gifts', selection())])
    await run()
    assert harness.metrics['turn'].status == 'completed'
    assert len(provider.calls) == 3 and len(search.calls) == 1
    assert sum(kind == 'tool_repair' for kind, _ in events) == 1
    assert sum(kind == 'cards' for kind, _ in events) == 1
    assert session.last_products[0]['product_id'] == '11'
    assert 'filters' not in provider.calls[-1]['tools'][0]['function']['parameters']['properties']


@pytest.mark.asyncio
async def test_repeated_bad_selection_fails_without_committing_filters_or_cards():
    run, search, provider, session, events, harness = setup([
        tool_events('gift_context', context()), tool_events('show_gifts', selection('999')), tool_events('show_gifts', selection('999'))])
    await run()
    assert harness.metrics['turn'].status == 'failed'
    assert len(provider.calls) == 3 and len(search.calls) == 1
    assert session.filters.max_price is None and session.history == [] and session.last_products == []
    assert not any(kind in {'text', 'cards'} for kind, _ in events)


@pytest.mark.asyncio
async def test_parallel_operations_are_acknowledged_without_executing_then_corrected():
    batch = tool_events('gift_context', context())
    second = copy.deepcopy(batch[0]); second['value'].update(index=1, id='second-call')
    second['value']['function']['arguments'] = json.dumps(context())
    batch.insert(2, second)
    run, search, provider, session, events, harness = setup([
        batch, tool_events('gift_context', context()), tool_events('show_gifts', selection())])
    await run()
    assert harness.metrics['turn'].status == 'completed' and len(search.calls) == 1
    messages = provider.calls[1]['messages']
    assert len(messages[-3]['tool_calls']) == 2
    assert [m['role'] for m in messages[-2:]] == ['tool', 'tool']
    assert {m['tool_call_id'] for m in messages[-2:]} == {'fixture-call', 'second-call'}


@pytest.mark.asyncio
async def test_transport_failure_is_not_retried_or_interpreted_as_no_products():
    run, search, provider, session, events, harness = setup([[ProviderError('provider_http_503')]])
    await run()
    assert harness.metrics['turn'].status == 'failed' and len(provider.calls) == 1
    assert search.calls == [] and session.history == []
    assert not any(kind in {'text', 'cards', 'tool_repair'} for kind, _ in events)


def test_completed_public_answers_are_available_but_partial_assertions_are_excluded():
    turns = [
        {'turnId': 'a', 'message': '첫 질문', 'status': 'COMPLETED', 'blocks': [{'type': 'text', 'text': '완료 답변'}]},
        {'turnId': 'b', 'message': '다음 질문', 'status': 'INTERRUPTED', 'blocks': [{'type': 'text', 'text': '미완료 단정'}]},
        {'turnId': 'c', 'message': '현재 질문', 'status': 'RUNNING', 'blocks': []}]
    history = history_messages(turns, 'c')
    assert {'role': 'assistant', 'content': '완료 답변'} in history
    assert all(m['content'] != '미완료 단정' for m in history)


@pytest.mark.parametrize('status', ['CANCELLED', 'FAILED', 'INTERRUPTED'])
def test_abandoned_request_is_quoted_with_status_and_can_be_explicitly_retried(status):
    request = '부모님 집들이 선물을 골라줘.'
    turns = [
        {'turnId': 'old', 'message': request, 'status': status,
         'blocks': [{'type': 'text', 'text': '미완료 추천'}]},
        {'turnId': 'new', 'message': '중지했던 질문을 다시 해줘.',
         'status': 'PROCESSING', 'blocks': []}]
    history = history_messages(turns, 'new')
    assert len(history) == 2
    assert status in history[0]['content'] and history[0]['content'].endswith(request)
    assert all(message['content'] != request for message in history)
    assert history[-1] == {'role': 'user', 'content': turns[-1]['message']}
    assert not any(message['role'] == 'assistant' for message in history)


@pytest.mark.asyncio
async def test_prepared_pool_selection_respects_private_exclusions_without_exposing_them():
    run, search, provider, session, events, harness = setup([
        tool_events('show_gifts', {'message':'조건에 맞는 상품이 없어요.','products':[]})])
    session.profile=Profile(preferences='private preference',disliked_category_ids=['1'],candidate_ids=['11'])
    async def emit(kind,value):events.append((kind,copy.deepcopy(value)))
    await harness.run(session,SimpleNamespace(message='추천해줘',model='fixture',intent_hint='gift'),'turn',emit)
    assert harness.metrics['turn'].status=='completed' and len(provider.calls)==1
    schema=provider.calls[0]['tools'][0]['function']['parameters']['properties']
    assert schema['products']['maxItems']==0 and 'filters' not in schema
    assert 'private preference' not in provider.calls[0]['messages'][0]['content']
    state=json.loads(provider.calls[0]['messages'][0]['content'].split('STATE_JSON\n',1)[1])
    assert state['filters']['exclude_category_ids']==[]
    assert session.last_products==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('source_mentions_other_brand',[False,True])
async def test_brand_identity_is_checked_against_the_selected_products_own_source(source_mentions_other_brand):
    claim=selection(reason='다른브랜드의 휴대용 음료 용기입니다.')
    scripts=[tool_events('gift_context',context()),tool_events('show_gifts',claim)]
    if not source_mentions_other_brand:scripts.append(tool_events('show_gifts',selection()))
    run,search,provider,session,events,harness=setup(scripts)
    original=search.search
    async def two_brands(request,snapshot_id):
        result=await original(request,snapshot_id)
        other=copy.deepcopy(result['hits'][0]);other.update(productId=22,brand='다른브랜드')
        if source_mentions_other_brand:result['hits'][0]['description']+=' 다른브랜드와 함께 만든 상품입니다.'
        result['hits'].append(other);return result
    search.search=two_brands
    await run()
    assert harness.metrics['turn'].status=='completed'
    assert len(provider.calls)==(2 if source_mentions_other_brand else 3)
    if not source_mentions_other_brand:
        feedback=json.loads(provider.calls[-1]['messages'][-1]['content'])
        assert feedback['error']['code']=='selected_reason_brand_mismatch'
        assert session.last_products[0]['reason']==selection()['products'][0]['reason']
