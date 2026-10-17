"""Protocol regressions only. Model quality uses separate actual remote replays."""
import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_three_gifts import three_setup
from app.gift_consult import ConsultHarness
from app.gift_core.provider import tool_events
from app.gift_protocol import manifest
from training_contract import validate_records
from decision_masking import omitted_prose_spans

CONTEXT = tool_events('gift_context', {'queries':['양말'], 'changes':{'max_price':10000}})


def setup(scripts):
    old,s,search,events,_ = three_setup(scripts)
    h = ConsultHarness(old.catalog,old.context,old.provider,turn_timeout=None)
    async def run(message,tid):
        events.clear()
        async def emit(kind,data): events.append((kind,copy.deepcopy(data)))
        await h.run(s,SimpleNamespace(message=message,intent_hint='auto',model='fixture'),tid,emit)
        return h.metrics[tid]
    return h,s,search,events,run


@pytest.mark.asyncio
async def test_search_inspection_commits_conditions_without_forcing_product_selection():
    h,s,search,events,run=setup([CONTEXT,tool_events('inspect_search',{}),tool_events('inspect_search',{})])
    assert (await run('어떤 브랜드가 나왔나','first')).status=='completed'
    text=''.join(v['delta'] for k,v in events if k=='text')
    assert '10,000원 이하' in text and '4개' in text and '전체 카탈로그' in text
    assert not any(k in ('cards','catalog') for k,_ in events)
    assert s.filters.max_price==10000 and s.last_search
    before=copy.deepcopy(s.last_search)
    assert (await run('검색 조건은?','next')).status=='completed'
    assert s.last_search==before and len(search.calls)==1
    result=json.loads([m for m in s.history if m['role']=='tool'][-1]['content'])
    assert sum(result['observedBrandCounts'].values())==4 and result['serverRendered']


@pytest.mark.asyncio
@pytest.mark.parametrize('tool',['show_gifts','list_gifts'])
async def test_final_intro_survives_tool_and_native_history_with_verified_conditions(tool):
    args={'product_ids':['12','11'],'message':'확인된 범위에서 골랐어요. 무게 정보는 없어요.'}
    if tool=='list_gifts': args['format']='brief'
    h,s,search,events,run=setup([CONTEXT,tool_events(tool,args),CONTEXT,tool_events(tool,args)])
    for tid in ['fresh','cached']:
        assert (await run('두 개 보여줘',tid)).status=='completed'
        text=''.join(v['delta'] for k,v in events if k=='text')
        assert args['message'] in text and '10,000원 이하' in text
        assert any(k in ('cards','catalog') for k,_ in events)
        assert s.history[-1]['content']==text
    assert len(search.calls)==1
    raw=[m for m in s.history if m.get('tool_calls')][-1]['tool_calls'][0]['function']
    assert json.loads(raw['arguments'])==args


@pytest.mark.asyncio
async def test_plain_selection_repair_then_brand_grouping_succeeds_without_second_repair():
    plain=[{'kind':'text','value':'브랜드 보고입니다.'},{'kind':'finish','value':'stop'}]
    args={'product_ids':['12','11','14'],'format':'brief','one_per_brand':True}
    h,s,_,events,run=setup([CONTEXT,plain,tool_events('list_gifts',args)])
    assert (await run('브랜드마다 하나','group')).status=='completed'
    assert h.metrics['group'].stages['tool_repairs']==1
    products=next(v['products'] for k,v in events if k=='catalog')
    assert [p['product_id'] for p in products]==['12']
    assert s.last_search['last_listed_product_ids']==['12']
    result=json.loads([m for m in s.history if m['role']=='tool'][-1]['content'])
    assert result['product_ids']==['12'] and result['requested_product_ids']==args['product_ids']
    assert result['groupingPolicy']=='first_selected_per_brand'


@pytest.mark.asyncio
async def test_grouping_cannot_hide_invalid_id_and_failure_preserves_state():
    bad=tool_events('list_gifts',{'product_ids':['12','999'],'format':'brief','one_per_brand':True,'message':'표시하면 안 됨'})
    h,s,_,events,run=setup([CONTEXT,bad,bad])
    before=copy.deepcopy(s.last_search)
    assert (await run('브랜드마다 하나','bad')).status=='failed'
    assert not any(k in ('text','cards','catalog') for k,_ in events)
    assert s.last_search==before and s.filters.max_price==20000


@pytest.mark.asyncio
async def test_latest_gallery_ordinal_not_old_recommendation():
    h,s,search,events,run=setup([CONTEXT,tool_events('show_gifts',{'product_ids':['11']}),
       CONTEXT,tool_events('list_gifts',{'product_ids':['14','12'],'format':'brief'}),
       tool_events('recall_gift',{'position':1})])
    await run('추천','cards');await run('다른 목록','list')
    assert (await run('첫 번째 이름과 가격','recall')).status=='completed'
    text=''.join(v['delta'] for k,v in events if k=='text')
    assert '상품 14' in text and '7,000원' in text and len(search.calls)==1


def test_native_training_registers_new_tools_and_masks_only_model_intro():
    p=manifest('stable-v4.6')
    for name,args in [('inspect_search',{}),('show_gifts',{'product_ids':['1'],'message':'안내'}),
                      ('list_gifts',{'product_ids':['1'],'format':'brief','message':'안내'})]:
        row={'harnessVersion':p['version'],'harnessSha256':p['sourceSha256'],'serverRenderedTextTarget':False,
             'completion':[{'role':'assistant','content':None,'tool_calls':[{'function':{'name':name,'arguments':args}}]}]}
        assert validate_records([row])==p
        native='<|tool_call>call:'+name+json.dumps(args,ensure_ascii=False)+'<tool_call|><|tool_response>'
        spans,fields=omitted_prose_spans(native)
        assert [native[a:b] for a,b in spans]==(['안내'] if 'message' in args else [])
        if name!='inspect_search':
            old=manifest('stable-v4.5')
            with pytest.raises(ValidationError):
                validate_records([{**row,'harnessVersion':old['version'],'harnessSha256':old['sourceSha256']}])



@pytest.mark.asyncio
async def test_cached_display_requires_context_and_changed_kind_searches_again():
    show=tool_events('show_gifts',{'product_ids':['12']})
    changed=tool_events('gift_context',{'queries':['다른 종류'],'changes':{}})
    h,s,search,events,run=setup([CONTEXT,show,show,changed,show])
    assert (await run('처음 추천','first')).status=='completed'
    assert not {'show_gifts','list_gifts'} & {t['function']['name'] for t in h.answer_tools(s)}
    assert (await run('다른 종류로 추천','next')).status=='completed'
    assert len(search.calls)==2 and s.last_queries==['다른 종류']
    assert any(k=='tool_repair' and v['feedback']['code']=='unavailable_tool' for k,v in events)


@pytest.mark.asyncio
async def test_names_only_reuse_preserves_order_and_does_not_add_conditions():
    names=tool_events('list_gifts',{'product_ids':['14','12'],'format':'names'})
    h,s,search,events,run=setup([CONTEXT,names,CONTEXT,names])
    await run('이름만','one');await run('그대로 이름만','two')
    text=''.join(v['delta'] for k,v in events if k=='text')
    assert '검색어:' not in text and '가격:' not in text
    assert text.index('상품 14') < text.index('상품 12')
    assert len(search.calls)==1 and h.metrics['two'].cache_hit


@pytest.mark.asyncio
async def test_inspection_reports_excluded_products_without_changing_selection():
    h,s,search,events,run=setup([CONTEXT,tool_events('show_gifts',{'product_ids':['11']}),
        tool_events('gift_context',{'queries':['양말'],'changes':{'exclude_product_ids':['11']}}),
        tool_events('inspect_search',{})])
    original_search=search.search
    async def filtered_search(request,snapshot_id):
        result=await original_search(request,snapshot_id)
        excluded=request['filters'].get('excludeProductIds',[])
        result['hits']=[p for p in result['hits'] if p['productId'] not in excluded]
        return result
    search.search=filtered_search
    assert (await run('양말 하나 골라줘','before')).status=='completed'
    assert (await run('방금 것은 빼고 어떤 조건인지 알려줘','after')).status=='completed'
    text=''.join(v['delta'] for k,v in events if k=='text')
    assert '제외 상품: 면 혼방 양말 상품 11' in text
    assert s.filters.exclude_product_ids==['11']
    assert not any(k in ('cards','catalog') for k,_ in events)
    result=h.inspect_result(s)
    assert result['filters']['exclude_product_ids']==['11']
    h.catalog.products.pop('11',None)
    assert '제외 상품: 상품 ID 11' in h.inspect_result(s)['text']
