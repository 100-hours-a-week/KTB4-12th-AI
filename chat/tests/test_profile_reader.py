from uuid import uuid4
import pytest
import httpx


@pytest.mark.asyncio
async def test_profile_reader_never_hides_authority_failures():
    from app.errors import APIError
    from app.profile_reader import ProfileReader
    calls=[];status=403
    def handler(request):calls.append(request);return httpx.Response(status,json={})
    reader=ProfileReader('http://profiler','synthetic',transport=httpx.MockTransport(handler))
    binding={'senderUserId':11,'recipientUserId':42}
    access={'grantId':str(uuid4()),'callerUserId':11,'recipientUserId':42,'sourceVersion':7}
    explicit={'status':'AVAILABLE','recipientUserId':42,'sourceVersion':7,'giftPreference':'명시한 합성 취향','dislikedCategories':[{'categoryId':11,'categoryName':'합성'}]}
    try:
        with pytest.raises(APIError):await reader.supplement(explicit,binding=binding,access=None)
        assert not calls
        for status in (401,403):
            with pytest.raises(APIError) as error:await reader.supplement(explicit,binding=binding,access=access)
            assert error.value.status==status
        for status in (404,409,503):
            got=await reader.supplement(explicit,binding=binding,access=access)
            assert got['data']==explicit and got['profileRead']['source']=='backend-explicit'
            with pytest.raises(APIError):await reader.supplement(None,binding=binding,access=access)
    finally:await reader.close()


@pytest.mark.asyncio
async def test_profile_reader_only_fills_missing_and_checks_origin_version_and_freshness():
    from app.errors import APIError
    from app.profile_reader import ProfileReader
    from datetime import datetime, timezone
    now=datetime(2026,10,17,tzinfo=timezone.utc)
    data={'source':'profiler','recipientUserId':42,'sourceVersion':7,'asOf':now.isoformat(),
          'preferredTags':[{'axis':'카테고리','tag':'커피'}],
          'dislikedTags':[{'axis':'브랜드','tag':'소니'}],
          'dislikedCategories':[{'categoryId':12,'categoryName':'음향'}]}
    contract={'protocol':'shared-catalog/1','catalogVersion':str(uuid4()),
              'productTagsRule':'vocab-v3','productTagsSha256':'a'*64}
    data.update(catalogVersion=contract['catalogVersion'],validatorVersion='tag3disliked-v1',
                tagTaxonomy={'productTagsRule':'vocab-v3','productTagsSha256':'a'*64,
                             'validatorVersion':'tag3disliked-v1'})
    reader=ProfileReader('http://profiler','synthetic',transport=httpx.MockTransport(lambda r:httpx.Response(200,json=data)))
    binding={'senderUserId':11,'recipientUserId':42}
    access={'grantId':str(uuid4()),'callerUserId':11,'recipientUserId':42,'sourceVersion':7}
    explicit={'status':'AVAILABLE','recipientUserId':42,'sourceVersion':7,'giftPreference':'명시한 취향',
              'dislikedCategories':[{'categoryId':11,'categoryName':'컵'}]}
    try:
        result=await reader.supplement(explicit,binding=binding,access=access,catalog_contract=contract,now=now)
        assert result['data']['giftPreference']=='명시한 취향' and 'preferredTags' not in result['data']
        assert result['data']['dislikedCategories']==explicit['dislikedCategories']
        assert result['data']['dislikedTags']==data['dislikedTags']
        none={'status':'NONE','recipientUserId':42,'sourceVersion':7,'giftPreference':None,'dislikedCategories':[],'dislikedTags':[]}
        result=await reader.supplement(none,binding=binding,access=access,catalog_contract=contract,now=now)
        assert result['data']==none
        result=await reader.supplement(None,binding=binding,access=access,catalog_contract=contract,now=now)
        assert result['data']['preferredTags']==data['preferredTags']
        data['recipientUserId']=43
        with pytest.raises(APIError) as error:await reader.supplement(explicit,binding=binding,access=access,now=now)
        assert error.value.status==403
        data['recipientUserId']=42;data['asOf']='2026-10-01T00:00:00Z'
        assert (await reader.supplement(explicit,binding=binding,access=access,now=now))['profileRead']['source']=='backend-explicit'
        with pytest.raises(APIError):await reader.supplement({'status':'AVAILABLE'},binding=binding,access=access,now=now)
        data.clear()
        with pytest.raises(APIError):await reader.supplement(explicit,binding=binding,access=access,now=now)
    finally:await reader.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['legacy', 'generation', 'vocabulary', 'validator', 'missing-catalog'])
async def test_unverified_inferred_likes_are_omitted_but_dislikes_block(mismatch):
    from datetime import datetime, timezone
    from app.errors import APIError
    from app.profile_reader import ProfileReader
    now=datetime(2026,10,17,tzinfo=timezone.utc)
    binding={'senderUserId':11,'recipientUserId':42}
    access={'grantId':str(uuid4()),'callerUserId':11,'recipientUserId':42,'sourceVersion':7}
    contract={'protocol':'shared-catalog/1','catalogVersion':str(uuid4()),
              'productTagsRule':'vocab-v3','productTagsSha256':'a'*64}
    data={'source':'profiler','recipientUserId':42,'sourceVersion':7,'asOf':now.isoformat(),
          'catalogVersion':contract['catalogVersion'],'validatorVersion':'tag3disliked-v1',
          'tagTaxonomy':{'productTagsRule':'vocab-v3','productTagsSha256':'a'*64,'validatorVersion':'tag3disliked-v1'},
          'preferredTags':[{'axis':'브랜드','tag':'합성'}],'dislikedTags':[],'dislikedCategories':[]}
    if mismatch=='legacy':data['tagTaxonomy']=None
    elif mismatch=='generation':data['catalogVersion']=str(uuid4())
    elif mismatch=='vocabulary':data['tagTaxonomy']['productTagsSha256']='b'*64
    elif mismatch=='validator':data['validatorVersion']='tag4-v1'
    elif mismatch=='missing-catalog':contract=None
    reader=ProfileReader('http://profiler','synthetic',transport=httpx.MockTransport(lambda r:httpx.Response(200,json=data)))
    try:
        result=await reader.supplement(None,binding=binding,access=access,catalog_contract=contract,now=now)
        assert 'preferredTags' not in result['data']
        assert result['profileRead']['tagCompatibility']=='unverified_inference_omitted'
        data['dislikedTags']=[{'axis':'브랜드','tag':'합성'}]
        with pytest.raises(APIError) as caught:
            await reader.supplement(None,binding=binding,access=access,catalog_contract=contract,now=now)
        assert caught.value.status==503
        # Current explicit BE decisions retain precedence, including []/null.
        explicit={'status':'NONE','recipientUserId':42,'sourceVersion':7,
                  'giftPreference':None,'dislikedTags':[],'dislikedCategories':[]}
        assert (await reader.supplement(explicit,binding=binding,access=access,
                                       catalog_contract=contract,now=now))['data']==explicit
    finally:await reader.close()
