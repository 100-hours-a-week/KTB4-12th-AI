"""Protocol tests; live model behavior is measured separately on Colab."""
import copy
import json
from types import SimpleNamespace

import pytest
from test_three_gifts import three_setup
from app.gift_core.provider import tool_events
from app.gift_dialogue import DialogueHarness
from app.gift_protocol import manifest
from training_contract import validate_records


def setup(scripts):
    old, session, search, events, _ = three_setup(scripts)
    harness = DialogueHarness(old.catalog, old.context, old.provider, turn_timeout=None)
    async def run(message):
        async def emit(kind, value): events.append((kind, copy.deepcopy(value)))
        await harness.run(session, SimpleNamespace(message=message, intent_hint='auto', model='fixture'), 'turn', emit)
        return harness.metrics['turn']
    return harness, session, search, events, run


@pytest.mark.asyncio
async def test_bad_argument_is_not_executed_and_model_can_correct_it():
    h, s, search, events, run = setup([
        tool_events('gift_context', {'queries':['양말'], 'changes':{'queries':['양말']}}),
        tool_events('gift_context', {'queries':['양말'], 'max_price':10000}),
        tool_events('show_gifts', {'product_ids':['11']}),
    ])
    result = await run('양말을 추천해줘')
    assert result.status == 'completed' and result.calls == 3
    assert len(search.calls) == 1 and s.filters.max_price == 10000
    feedback = next(v['feedback'] for k, v in events if k == 'tool_repair')
    assert feedback['fields'] == [{'location':'changes', 'type':'extra_forbidden'}]
    calls = [json.loads(m['tool_calls'][0]['function']['arguments']) for m in s.history if m.get('tool_calls')]
    assert calls[0] == {'queries':['양말'], 'max_price':10000}


@pytest.mark.asyncio
async def test_model_can_ask_after_rejection_without_committing_rejected_changes():
    question = '연결할 기기의 규격을 알려주실 수 있나요?'
    h, s, search, events, run = setup([
        tool_events('gift_context', {'queries':['케이블'], 'changes':{'queries':['케이블'], 'max_price':9000}}),
        [{'kind':'text', 'value':question}, {'kind':'finish', 'value':'stop'}],
    ])
    assert (await run('연결용 케이블을 찾고 있어')).status == 'completed'
    assert not search.calls and s.filters.max_price == 20000 and not s.last_search
    assert ''.join(v['delta'] for k, v in events if k == 'text') == question


def test_shared_training_protocol_keeps_native_arguments_and_no_prose_target():
    p = manifest('stable-v4.7')
    row = {'harnessVersion':p['version'], 'harnessSha256':p['sourceSha256'], 'serverRenderedTextTarget':False,
           'completion':[{'role':'assistant', 'content':None, 'tool_calls':[
               {'function':{'name':'gift_context', 'arguments':{'queries':['우산'], 'max_price':10000}}}]}]}
    assert validate_records([row]) == p


def test_declared_price_replacement_and_omission_semantics():
    h, s, _, _, _ = setup([])
    request = SimpleNamespace(message='This text is not parsed by the executor.')
    schema = h.context_tool(s)['function']['parameters']
    # The tool command controls the transition; no user-keyword inference.
    args = h.parse_context({'queries':['우산'], 'min_price':50000}, schema, s, request)
    assert args.filters.min_price == 50000 and args.filters.max_price is None
    args = h.parse_context({'queries':['우산']}, schema, s, request)
    assert args.filters.min_price is None and args.filters.max_price == 20000
    s.filters.min_price = 5000
    args = h.parse_context({'queries':['우산'], 'min_price':5000, 'max_price':None}, schema, s, request)
    assert args.filters.min_price == 5000 and args.filters.max_price is None
    args = h.parse_context({'queries':['우산'], 'max_price':None}, schema, s, request)
    assert args.filters.min_price is None and args.filters.max_price is None
