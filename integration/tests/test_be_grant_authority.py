from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
import httpx
import pytest

from profiling import profile_read


def test_be_scoped_grant_is_fail_closed_and_never_uses_legacy_grants(monkeypatch):
    monkeypatch.setenv('PROFILING_BE_GRANTS_ENABLED','true')
    body=profile_read.ProfileReadRequest(grantId=uuid4(),callerUserId=11,recipientUserId=42,
        sourceVersion=7,conversationId=uuid4(),turnId=uuid4())
    calls=[];status=200;expires=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
    def handle(request):
        calls.append(request)
        return httpx.Response(status,json={'data':{'expiresAt':expires}})
    client=httpx.Client(transport=httpx.MockTransport(handle),base_url='http://backend')
    monkeypatch.setattr(profile_read.httpx,'Client',lambda **kw:client)
    class Settings: BACKEND_BASE_URL='http://backend'
    monkeypatch.setattr(profile_read,'get_settings',lambda:Settings())
    class Result:
        def mappings(self):return self
        def first(self):return {'source_version':7,'guarded_input':{'giftPreference':'합성 취향','dislikedCategories':[]},
            'successful_analysis':{'sourceVersion':7,'analyzedAt':datetime.now(timezone.utc).isoformat(),
                'preferredTags':[],'dislikedTags':[],'tagStatus':'ok','catalogVersion':'catalog',
                'validatorVersion':'tag3disliked-v1','tagTaxonomy':None}}
    class Engine:
        def connect(self):return nullcontext(self)
        def execute(self,sql,args):
            assert 'recipient_inputs' in str(sql) and 'read_grants' not in str(sql)
            return Result()
    # Use a reusable context manager; closing a client on each policy attempt
    # would test httpx lifecycle instead of authority failures.
    monkeypatch.setattr(profile_read.httpx,'Client',lambda **kw:nullcontext(client))
    assert profile_read.read_scoped_profile(Engine(),body,'fixture')['sourceVersion']==7
    assert calls[-1].url.path=='/api/internal/v1/profile-grants/verify'
    assert calls[-1].headers['authorization']=='Bearer fixture'
    for status in (400,401,403,404,409,500,503):
        with pytest.raises(HTTPException):profile_read.read_scoped_profile(Engine(),body,'fixture')
    status=200;expires=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    with pytest.raises(HTTPException):profile_read.read_scoped_profile(Engine(),body,'fixture')
    monkeypatch.setenv('PROFILING_BE_GRANTS_ENABLED','false')
    count=len(calls)
    with pytest.raises(HTTPException):profile_read.read_scoped_profile(Engine(),body,'fixture')
    assert len(calls)==count
    client.close()
