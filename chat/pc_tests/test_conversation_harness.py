"""Model-led answers and truthful display memory, using CPU fixtures."""
import copy
import json
from types import SimpleNamespace

import pytest

from app.gift_conversation import ConversationHarness
from app.gift_protocol import DEFAULT, manifest
from test_clean_io_harness import setup as old_setup, query, show, tool_events
from test_compact_harness import run
from training_contract import validate_records


def text(value):
    return [{'kind':'text','value':value},{'kind':'finish','value':'stop'}]


def setup(scripts):
    old,s,search,p = old_setup(scripts)
    return ConversationHarness(old.catalog,old.context,p,turn_timeout=None),s,search,p


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', ['후보 설명에는 무게가 없어서 확인할 수 없어요.', 'USB-C와 라이트닝 중 어느 연결 방식인가요?'])
async def test_factual_answer_or_question_after_search_completes_without_forced_display(reply):
    h,s,search,p = setup([query(),text(reply)])
    events = await run(h,s,'조회해서 알려줘','facts')
    assert ''.join(v['delta'] for k,v in events if k=='text') == reply
    assert not any(k in ('cards','catalog','tool_repair') for k,v in events)
    assert len(p.calls)==2 and s.last_search['products']
    assert s.last_displayed_products == []


@pytest.mark.asyncio
async def test_empty_success_is_passed_to_model_without_widening_budget():
    h,s,search,p = setup([tool_events('gift_context',{'queries':['양말'],'changes':{'max_price':100}}),
        text('100원 이하로 조회한 후보는 없어요. 예산을 바꿔볼까요?')])
    async def empty(request,snapshot_id):
        search.calls.append(request)
        return {'snapshotId':snapshot_id,'hits':[]}
    search.search = empty
    events = await run(h,s,'100원 이하로 찾아줘','empty')
    wire = json.loads(p.calls[1]['messages'][-1]['content'])
    assert wire['products']==[] and wire['filters']['max_price']==100
    assert s.filters.max_price==100 and len(search.calls)==1
    assert {t['function']['name'] for t in p.calls[1]['tools']} == {'inspect_search'}
    assert any(v['delta'].startswith('100원 이하') for k,v in events if k=='text')


@pytest.mark.asyncio
async def test_search_failure_is_not_presented_as_empty_results():
    h,s,search,p = setup([query()])
    async def broken(*a,**kw):
        raise ConnectionError('fixture search disconnected')
    search.search = broken
    events=[]
    async def emit(k,v):events.append((k,v))
    await h.run(s,SimpleNamespace(message='찾아줘',intent_hint='auto',model='fixture'),'fail',emit)
    assert h.metrics['fail'].status=='failed' and len(p.calls)==1
    assert not s.last_search and not s.last_displayed_products
    assert not any(k in ('text','cards','catalog') for k,v in events)


@pytest.mark.asyncio
async def test_rejected_call_can_end_in_clarification_instead_of_forced_retry():
    h,s,_,p = setup([tool_events('gift_context',{'queries':['양말'],'changes':{'max_price':'알 수 없음'}}),
                     text('예산 상한을 얼마로 정할까요?')])
    await run(h,s,'그 예산으로 찾아줘','clarify')
    assert len(p.calls)==2 and s.filters.max_price==20000 and not s.last_search
    assert h.metrics['clarify'].stages['tool_repairs']==1


@pytest.mark.asyncio
@pytest.mark.parametrize('finish_search',[tool_events('inspect_search',{}),text('조회 조건을 확인했어요.')])
async def test_search_only_does_not_replace_actual_display_order(finish_search):
    h,s,_,p = setup([query(),show('11'),query(),tool_events('list_gifts',{'product_ids':['14','12']}),
                     query(),finish_search,tool_events('recall_gift',{'position':1})])
    for i,msg in enumerate(['추천 하나','이름 두 개','검색 현황 확인']):
        await run(h,s,msg,str(i))
    assert [r['product_id'] for r in h.displayed(s)]==['14','12']
    assert s.last_products[0]['product_id']=='11'
    assert s.last_search['last_listed_product_ids']==[]
    events = await run(h,s,'첫 번째 얼마야?','recall')
    assert any('7,000원' in v['delta'] for k,v in events if k=='text')
    state=json.loads(p.calls[-1]['messages'][0]['content'].split('STATE_JSON\n')[1])
    assert [r['product_id'] for r in state['last_displayed_products']]==['14','12']


@pytest.mark.asyncio
async def test_previous_card_specs_survive_new_cards_and_history_projection():
    h,s,search,p=setup([query(),show('11'),query(),show('12')])
    await run(h,s,'첫 번째 추천','old')
    description=s.last_products[0]['description']
    assert description
    await run(h,s,'새 추천','new')
    before=copy.deepcopy(s.history)
    history=h.prompt_history(s)
    facts=[row for m in history if m['role']=='tool' for row in json.loads(m['content']).get('products',[])]
    assert any(row['product_id']=='11' and row['description']==description for row in facts)
    assert s.history==before


@pytest.mark.asyncio
async def test_display_memory_survives_more_than_four_chat_turns_and_failed_display():
    bad=tool_events('list_gifts',{'product_ids':['missing']})
    h,s,_,p=setup([query(),tool_events('list_gifts',{'product_ids':['14','12']}),
                   *[text('안녕하세요.') for _ in range(5)],bad,bad])
    await run(h,s,'이름 두 개','display')
    for i in range(5):await run(h,s,'안녕',f'chat{i}')
    assert not any(m['role']=='tool' for m in s.history)
    assert [r['product_id'] for r in h.displayed(s)]==['14','12']
    before=copy.deepcopy(s.last_displayed_products)
    async def emit(*a):pass
    await h.run(s,SimpleNamespace(message='사진 다시',intent_hint='auto',model='fixture'),'bad',emit)
    assert h.metrics['bad'].status=='failed' and s.last_displayed_products==before


@pytest.mark.asyncio
async def test_new_protocol_training_keeps_native_decisions_and_text_answers():
    h,s,_,p=setup([query(),text('설명에 무게 정보가 없어요.')])
    await run(h,s,'상품 무게 알려줘','training')
    protocol=manifest(DEFAULT)
    assert DEFAULT==h.variant=='stable-v4.12'
    assert 'gift_conversation.py' in protocol['sourceHashes']
    assert 'gift_conversation.py' not in manifest('stable-v4.11')['sourceHashes']
    row={'harnessVersion':DEFAULT,'harnessSha256':protocol['sourceSha256'],'serverRenderedTextTarget':False,
         'prompt':p.calls[1]['messages'],'tools':p.calls[1]['tools'],
         'completion':[{'role':'assistant','content':'설명에 무게 정보가 없어요.'}]}
    assert validate_records([row])==protocol
