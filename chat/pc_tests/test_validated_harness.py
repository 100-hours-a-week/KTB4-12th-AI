from types import SimpleNamespace
import copy
import pytest
from test_guided_harness import setup
from app.gift_validated import ValidatedHarness
from app.gift_core.provider import tool_events
from app.gift_core.contracts import Filters


@pytest.mark.asyncio
async def test_typo_is_not_executed_or_auto_fuzzy_matched_and_one_correction_succeeds():
    old,s,search,p,events,_=setup([
        tool_events('gift_context',{'queries':['양말'],'changes':{'brands':['라코스톄'],'max_price':10000}}),
        tool_events('gift_context',{'queries':['양말'],'changes':{'brands':['라코스테'],'max_price':10000}}),
        tool_events('show_gifts',{'product_ids':['11']})])
    h=ValidatedHarness(old.catalog,old.context,p,turn_timeout=None)
    async def emit(k,v):events.append((k,copy.deepcopy(v)))
    await h.run(s,SimpleNamespace(message='라코스테만 만원 이내로',intent_hint='auto',model='fixture'),'t',emit)
    assert h.metrics['t'].status=='completed' and h.metrics['t'].stages['tool_repairs']==1
    assert len(search.calls)==1 and search.calls[0]['filters']['brands']==['라코스테']
    feedback=next(v['feedback'] for k,v in events if k=='tool_repair')
    assert feedback['code']=='unknown_catalog_brand' and '라코스테' in feedback['catalog_suggestions']
    assert s.filters.brands==['라코스테'] and s.filter_sources['brands']['value']==['라코스테']


@pytest.mark.asyncio
async def test_repeated_unknown_brand_leaves_state_and_actual_search_untouched():
    bad=tool_events('gift_context',{'queries':['양말'],'changes':{'brands':['존재하지 않는 브랜드'],'max_price':100}})
    old,s,search,p,events,_=setup([bad,bad],Filters(max_price=20000))
    h=ValidatedHarness(old.catalog,old.context,p,turn_timeout=None)
    async def emit(k,v):events.append((k,v))
    await h.run(s,SimpleNamespace(message='새 브랜드로',intent_hint='auto',model='fixture'),'t',emit)
    assert h.metrics['t'].status=='failed' and search.calls==[]
    assert s.filters.max_price==20000 and not hasattr(s,'filter_sources')
    assert len(s.history)==1 and s.history[0]['content'].startswith('[과거 요청 상태: FAILED')


def test_brand_aliases_and_global_brands_outside_pool_are_still_valid():
    old,s,_,p,_,_=setup([])
    h=ValidatedHarness(old.catalog,old.context,p)
    args=h.parse_context({'queries':['양말'],'changes':{'exclude_brands':['폴로 랄프 로렌']}},h.context_tool(s),s,None)
    assert args.filters.exclude_brands==['폴로 랄프 로렌(의류)']
