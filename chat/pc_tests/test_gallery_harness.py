"""Synthetic gallery regressions: transport and invariants, not model quality."""
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from test_three_gifts import three_setup
from app.gift_gallery import GalleryHarness, VisualCatalogList
from app.gift_core.provider import tool_events
from app.models import CardsBlock, CatalogBlock, Turn
from app.gift_protocol import manifest
from training_contract import validate_records
from decision_masking import omitted_prose_spans


def setup(scripts):
    old, s, search, events, _ = three_setup(scripts)
    h = GalleryHarness(old.catalog, old.context, old.provider, turn_timeout=None)
    async def run(message, tid):
        events.clear()
        async def emit(kind, data): events.append((kind, copy.deepcopy(data)))
        await h.run(s, SimpleNamespace(message=message, intent_hint='auto', model='fixture'), tid, emit)
        return h.metrics[tid]
    return h, s, search, events, run


CONTEXT = tool_events('gift_context', {'queries': ['양말'], 'changes': {'max_price': 10000}})
NAMES = tool_events('list_gifts', {'product_ids': ['14', '11', '12', '13'], 'format': 'names'})
BRIEF = tool_events('list_gifts', {'product_ids': ['14', '11', '12', '13'], 'format': 'brief'})


@pytest.mark.asyncio
async def test_names_then_photos_rehydrates_selected_order_after_reload_without_search():
    h,s,search,events,run = setup([CONTEXT,NAMES,BRIEF])
    assert (await run('네 개 이름만', 'names')).status == 'completed'
    assert not any(k in ('cards','catalog') for k,_ in events)
    assert '면 혼방 양말 상품 14' in ''.join(v['delta'] for k,v in events if k=='text')
    s.last_search = json.loads(json.dumps(s.last_search))
    h.catalog.products.clear()
    assert (await run('상품 사진도 보여줘', 'photos')).status == 'completed'
    photos = next(v['products'] for k,v in events if k=='catalog')
    assert [p['product_id'] for p in photos] == ['14','11','12','13']
    assert all('image' in p and p['evidence']['serverRendered'] for p in photos)
    assert len(search.calls)==1 and not s.last_products and s.filters.max_price==10000
    assert h.metrics['photos'].first_card_ms is not None
    history = json.loads([m for m in s.history if m['role']=='tool'][-1]['content'])
    assert history['presentation']=='catalog_with_photos' and history['serverRendered']
    assert history['product_ids']==['14','11','12','13']


@pytest.mark.asyncio
async def test_fresh_brief_list_uses_actual_catalog_rows_and_new_price():
    h,s,_,events,run=setup([CONTEXT,BRIEF])
    assert (await run('목록과 사진', 'fresh')).status=='completed'
    assert len(next(v['products'] for k,v in events if k=='catalog'))==4
    assert s.filters.max_price==10000 and not s.last_products


@pytest.mark.asyncio
async def test_brand_duplicates_rejected_then_repaired_without_duplicate_display():
    bad=tool_events('list_gifts',{'product_ids':['11','12'],'format':'brief','one_per_brand':True})
    good=tool_events('list_gifts',{'product_ids':['12'],'format':'brief','one_per_brand':True})
    h,s,search,events,run=setup([CONTEXT,bad,good])
    m=await run('브랜드별 하나씩', 'repair')
    assert m.status=='completed' and m.stages['tool_repairs']==1
    error=next(v['feedback'] for k,v in events if k=='tool_repair')
    assert error['code']=='duplicate_selected_brand' and error['duplicate_product_ids']==[['11','12']]
    assert [p['product_id'] for p in next(v['products'] for k,v in events if k=='catalog')]==['12']
    assert len(search.calls)==1


@pytest.mark.asyncio
async def test_failed_duplicate_repair_does_not_commit_text_or_state():
    bad=tool_events('list_gifts',{'product_ids':['11','12'],'format':'brief','one_per_brand':True})
    plain=[{'kind':'text','value':'사진을 보여줄 수 없어요.'},{'kind':'finish','value':'stop'}]
    h,s,_,events,run=setup([CONTEXT,bad,plain])
    before=copy.deepcopy(s.last_search)
    assert (await run('브랜드별 하나씩','bad')).status=='failed'
    assert not any(k in ('text','cards','catalog') for k,_ in events)
    assert s.last_search==before and s.filters.max_price==20000


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['missing','filters','snapshot'])
async def test_changed_hydration_fails_closed_without_using_stale_rows(failure):
    h,s,search,events,run=setup([CONTEXT,NAMES,BRIEF,BRIEF])
    await run('이름만','names');before=copy.deepcopy(s.last_search)
    original=search.get_products
    async def lookup(ids,snapshot_id):
        data=await original(ids,snapshot_id)
        if failure=='missing':data['products']=data['products'][:1]
        elif failure=='filters':data['products'][0]['price']=999999
        else:data['snapshotId']='other'
        return data
    search.get_products=lookup
    assert (await run('사진 보여줘','bad')).status=='failed'
    assert not any(k in ('text','cards','catalog') for k,_ in events) and s.last_search==before


@pytest.mark.asyncio
async def test_gallery_does_not_replace_recommendation_recall():
    h,s,search,events,run=setup([CONTEXT,tool_events('show_gifts',{'product_ids':['12','11']}),BRIEF,
                               tool_events('recall_gift',{'position':2})])
    await run('두 개 추천','cards');previous=copy.deepcopy(s.last_products)
    await run('전체 사진','gallery')
    assert s.last_products==previous and len(s.last_search['last_listed_product_ids'])==4
    assert (await run('추천 카드 두 번째 가격','recall')).status=='completed'
    assert '5,500원' in ''.join(v['delta'] for k,v in events if k=='text') and len(search.calls)==1


def test_display_dto_keeps_three_card_limit_and_thirty_list_limit():
    rows=[{'productId':i,'reason':'상품 정보'} for i in range(1,31)]
    block=CatalogBlock.model_validate({'type':'catalog','products':rows})
    with pytest.raises(ValidationError):
        Turn.model_validate({'turnId':str(uuid4()),'status':'COMPLETED','message':'사진',
            'blocks':[block.model_dump(by_alias=True)],'lastEventId':4})



def test_new_brand_decision_is_strict_and_preserves_every_native_target_token():
    args={'product_ids':['11','12'],'format':'brief','one_per_brand':True}
    p=manifest('stable-v4.5')
    row={'harnessVersion':p['version'],'harnessSha256':p['sourceSha256'],'serverRenderedTextTarget':False,
         'completion':[{'role':'assistant','content':None,'tool_calls':[{'function':{'name':'list_gifts','arguments':args}}]}]}
    assert validate_records([row])==p
    old=manifest('stable-v4.4')
    with pytest.raises(ValidationError):
        validate_records([{**row,'harnessVersion':old['version'],'harnessSha256':old['sourceSha256']}])
    target='<|tool_call>call:list_gifts{product_ids:["11","12"],format:"brief",one_per_brand:true}<tool_call|><|tool_response>'
    assert omitted_prose_spans(target)==([],[])
    for value in ('true',1,None):
        with pytest.raises(ValidationError):VisualCatalogList.model_validate({**args,'one_per_brand':value})
    with pytest.raises(ValueError):omitted_prose_spans(target.replace('one_per_brand:true','one_per_brand:"true"'))
