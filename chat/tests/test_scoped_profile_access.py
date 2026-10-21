import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.errors import APIError
from app.models import PutTurn
from app.profile_reader import ProfileReader


@pytest.mark.asyncio
async def test_scoped_read_binds_authority_to_exact_conversation_turn_and_taxonomy():
    cid, tid = str(uuid4()), str(uuid4())
    access = dict(grantId=str(uuid4()), callerUserId=11, recipientUserId=42, sourceVersion=7)
    binding = dict(senderUserId=11, recipientUserId=42)
    contract = dict(protocol='shared-catalog/1', catalogVersion=str(uuid4()),
                    productTagsRule='vocab-v3', productTagsSha256='a'*64)
    now = datetime.now(timezone.utc)
    data = dict(source='profiler', recipientUserId=42, sourceVersion=7, asOf=now.isoformat(),
                preferredTags=[dict(axis='카테고리', tag='차')], dislikedTags=[dict(axis='브랜드', tag='소니')],
                dislikedCategories=[], catalogVersion=contract['catalogVersion'], validatorVersion='tag3disliked-v1',
                tagTaxonomy=dict(productTagsRule='vocab-v3',productTagsSha256='a'*64,validatorVersion='tag3disliked-v1'),
                grantExpiresAt=(now+timedelta(seconds=90)).isoformat())
    calls = []
    status = 200
    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(status,json=data)
    reader=ProfileReader('http://profiler','fixture',transport=httpx.MockTransport(handle))
    async def read(override=None):
        return await reader.snapshot(binding=binding,access=access if override is None else override,
            catalog_contract=contract,conversation_id=cid,turn_id=tid)
    try:
        with pytest.raises(APIError): await read(access|{'callerUserId':12})
        with pytest.raises(APIError): await read(access|{'recipientUserId':43})
        assert not calls
        result=await read()
        assert calls==[access|dict(conversationId=cid,turnId=tid)]
        assert result['analysisCurrent'] and result['inputSourceVersion']==7
        assert result['data']['preferredTags']==data['preferredTags']
        assert result['profileRead']['tagCompatibility']=='verified'
        for status in (401,403,404,409,503):
            with pytest.raises(APIError): await read()
        status=200
        data['grantExpiresAt']=(now-timedelta(seconds=1)).isoformat()
        with pytest.raises(APIError): await read()
        data['grantExpiresAt']=(now+timedelta(hours=1)).isoformat()
        with pytest.raises(APIError): await read()
        data['grantExpiresAt']=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
        data['tagTaxonomy']=None
        with pytest.raises(APIError): await read()
    finally: await reader.close()


@pytest.mark.parametrize('patch', [dict(callerUserId=True),dict(sourceVersion=0),dict(grantId='invalid'),dict(recipientUserId=0)])
def test_profile_access_wire_contract_is_strict(patch):
    access=dict(grantId=str(uuid4()),callerUserId=11,recipientUserId=42,sourceVersion=7)|patch
    with pytest.raises(ValidationError): PutTurn.model_validate(dict(message='합성',profileAccess=access))
