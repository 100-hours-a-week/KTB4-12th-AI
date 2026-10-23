"""Free synthetic model-input proof. No BE grant API, model or database calls."""
import copy
import json
from types import SimpleNamespace

import pytest
from app.errors import APIError
from app.profile import recipient_model_context
from app.turn_report import report
from test_low_latency import run, tool, context, show, text


def snapshot(raw='합성 취향: 차를 좋아하고 소니는 싫습니다.', version=3, recipient=42):
    return {'source':'AI_PROFILE_STORE','status':'AVAILABLE','analysisCurrent':True,
        'analysisStatus':'COMPLETED','inputSourceVersion':version,'analysisSourceVersion':version,
        'data':{'recipientUserId':recipient,'sourceVersion':version,'giftPreference':raw,
            'dislikedCategories':[{'categoryId':20,'categoryName':'식품'}],
            'preferredTags':[{'axis':'카테고리','tag':'독서'}],
            'dislikedTags':[{'axis':'브랜드','tag':'소니'}]}}


def test_only_gift_fields_projected_with_exact_current_version():
    value=snapshot();value['data'].update(address='OMIT_ADDRESS',reviews=['OMIT_REVIEW'])
    projected=recipient_model_context(value)
    assert projected['giftPreference']==value['data']['giftPreference']
    assert projected['sourceVersion']==3
    assert 'recipientUserId' not in projected
    assert 'OMIT_' not in json.dumps(projected)
    for field in ('inputSourceVersion','analysisSourceVersion'):
        for bad in (None,2,True):
            changed=copy.deepcopy(value);changed[field]=bad
            assert recipient_model_context(changed) is None


@pytest.mark.asyncio
async def test_natural_text_reaches_model_but_not_public_report_or_logs(caplog):
    raw='SYNTHETIC_NATURAL_CANARY: 책을 올리는 받침을 좋아하고 소니 제품은 싫어요.'
    receipt,sent,store,search,cid,tid=await run(
        [tool('gift_context',context()),tool('show_gifts',show())],stored=snapshot(raw))
    assert all(raw in json.dumps(request['messages'],ensure_ascii=False) for request in sent)
    assert receipt['selectedProducts'][0]['productId']==101
    assert all(20 in request['filters']['excludeCategoryIds'] for request in search.queries)
    assert raw not in json.dumps(report(cid,store.public(store.states[tid]),receipt),ensure_ascii=False)
    assert raw not in caplog.text
    assert raw not in json.dumps(receipt['diagnostics'],ensure_ascii=False)
    assert {tool['function']['name'] for request in sent for tool in request['tools']} <= {
        'gift_context','inspect_search','list_gifts','recall_gift','show_gifts'}


@pytest.mark.asyncio
@pytest.mark.parametrize('state',['PENDING','PROCESSING','FAILED','STALE'])
async def test_incomplete_profile_stops_before_model(state):
    value=snapshot();value.update(analysisCurrent=False,analysisStatus=state,analysisSourceVersion=2)
    packets=[text('must not execute')]
    with pytest.raises(APIError,match='최신 취향 분석'):
        await run(packets,stored=value)
    assert len(packets)==1


@pytest.mark.asyncio
async def test_wrong_recipient_is_rejected_before_model():
    packets=[text('must not execute')]
    with pytest.raises(ValueError,match='recipient binding'):
        await run(packets,stored=snapshot(recipient=999))
    assert len(packets)==1


@pytest.mark.asyncio
async def test_missing_profile_does_not_reuse_previous_recipient_text():
    previous={'agent':'gift-tool-loop-v1','lowLatencyState':{'recipientIdentity':'OTHER',
              'recipient_preferences':{'giftPreference':'OTHER_RECIPIENT_CANARY'}}}
    receipt,sent,*_=await run([text('안녕하세요.')],old=previous,
                            stored={'source':'AI_PROFILE_STORE','status':'NOT_PROVIDED'})
    assert 'OTHER_RECIPIENT_CANARY' not in json.dumps(sent)
    assert receipt['recipientProfile']['status']=='NOT_PROVIDED'


@pytest.mark.asyncio
async def test_followup_uses_current_snapshot_not_cached_profile():
    previous={'agent':'gift-tool-loop-v1','lowLatencyState':{
        'recipient_preferences':{'giftPreference':'OLD_PROFILE_CANARY'}}}
    raw='NEW_PROFILE_CANARY: 책상에 두는 책받침을 좋아해요.'
    receipt,sent,*_=await run([tool('gift_context',context()),tool('show_gifts',show())],
                            old=previous,stored=snapshot(raw,version=4))
    assert raw in json.dumps(sent,ensure_ascii=False)
    assert 'OLD_PROFILE_CANARY' not in json.dumps(sent)
    assert receipt['recipientProfile']['inputSourceVersion']==4


def test_other_recipient_scope_hides_original_model_context():
    from app.gift_adapter import CatalogAdapter,ContextAdapter
    from app.gift_core.contracts import Profile,Filters
    from app.gift_core.harness import Session
    from app.gift_core.provider import ScriptedProvider
    from app.integrated_harness import IntegratedHarness
    catalog=CatalogAdapter(None,{'snapshotId':'synthetic','categories':[]},{})
    harness=IntegratedHarness(catalog,ContextAdapter(catalog),ScriptedProvider([]),turn_timeout=None)
    session=Session('synthetic',Profile(),Filters(recipient_scope='other'))
    session.recipient_preferences=recipient_model_context(snapshot())
    request=SimpleNamespace(message='다른 사람에게 추천',intent_hint='auto',model='synthetic')
    assert 'recipient_preferences' not in harness.state(session,request,None)


@pytest.mark.asyncio
async def test_existing_service_auth_is_required_for_profile_enabled_chat(monkeypatch):
    from app import main
    from fastapi.security import HTTPAuthorizationCredentials
    monkeypatch.setattr(main,'current_service_token',lambda:'synthetic-service-only')
    for credentials in (None,HTTPAuthorizationCredentials(scheme='Bearer',credentials='wrong')):
        with pytest.raises(APIError) as denied:await main.service(credentials)
        assert denied.value.status==401
    await main.service(HTTPAuthorizationCredentials(scheme='Bearer',credentials='synthetic-service-only'))


def test_turn_cannot_supply_an_arbitrary_recipient_or_new_grant_contract():
    from pydantic import ValidationError
    from app.models import PutTurn
    assert PutTurn.model_validate({'message':'합성 질문'}).message=='합성 질문'
    for extra in ({'recipientUserId':999},{'profileAccess':{'grantId':'arbitrary'}}):
        with pytest.raises(ValidationError):PutTurn.model_validate({'message':'합성 질문',**extra})
