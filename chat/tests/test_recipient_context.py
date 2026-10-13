import asyncio
import copy
import os
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models import PutTurn
from app.errors import APIError
from app.profile import profile_context


def snapshot(**changes):
    return {'recipientUserId':42, 'status':'AVAILABLE', 'sourceVersion':8,
            'giftPreference':'차를 좋아합니다.',
            'dislikedCategories':[{'categoryId':12,'categoryName':'향수'}], **changes}


@pytest.mark.parametrize('change', [
    {'recipientUserId':True}, {'sourceVersion':True}, {'sourceVersion':-1},
    {'sourceVersion':None}, {'giftPreference':'x'*501},
    {'dislikedCategories':[{'categoryId':12,'categoryName':'향수'}]*2},
    {'status':'NONE'}, {'status':'UNAVAILABLE'}, {'preferredTags':[]},
])
def test_invalid_context_is_rejected(change):
    with pytest.raises(ValidationError):PutTurn.model_validate({'message':'test','recipientContext':snapshot(**change)})


def test_empty_unavailable_and_omitted_are_distinct():
    none=snapshot(status='NONE',giftPreference=None,dislikedCategories=[])
    unavailable={**none,'status':'UNAVAILABLE','sourceVersion':None}
    for data in [none,unavailable]:
        assert PutTurn.model_validate({'message':'test','recipientContext':data}).recipient_context
    assert PutTurn.model_validate({'message':'test'}).recipient_context is None
    assert '조회하지 못했다' in profile_context({'source':'BACKEND_REQUEST','status':'UNAVAILABLE','data':unavailable})
    assert '재사용하지 않는다' in profile_context({'source':'BACKEND_REQUEST','status':'NONE','data':none})


@pytest.mark.parametrize('mode',['agent','baseline'])
async def test_prompt_and_trace_use_stored_turn_snapshot_without_backend_client(monkeypatch,mode):
    from app.agent import AgentRuntime
    from app.runtime import Runtime
    monkeypatch.setenv('OPENROUTER_API_KEY','test-only')
    # Old configuration must not enable a reverse call, even when incomplete.
    monkeypatch.setenv('BACKEND_PROFILE_URL','https://must-not-call.invalid')
    monkeypatch.delenv('BACKEND_PROFILE_TOKEN',raising=False)
    class Store:
        def __init__(self):self.trace={};self.events=[]
        async def call(self,method,**args):
            if method=='recipient_context':return copy.deepcopy(snapshot())
            if method=='read':return {'turns':[{'turnId':'t','message':'선물 추천','status':'PROCESSING','blocks':[]}]}
            if method=='context':self.trace=copy.deepcopy(args.get('value',{}));return {}
            if method=='emit':self.events.append((args['kind'],args['payload']));return True
            raise AssertionError(method)
    class Search:
        async def metadata(self):return {'snapshotId':'s','categories':[]}
    store=Store();runtime=(AgentRuntime if mode=='agent' else Runtime)(store,Search(),'owner')
    async def complete(messages,*args,**kw):
        assert '차를 좋아합니다' in messages[0]['content'] and '향수' in messages[0]['content']
        return {'search':False,'query':'','filters':{}}
    async def stream(cid,tid,receipt,request):
        assert '차를 좋아합니다' not in request['messages'][0]['content']
        assert '향수' not in request['messages'][0]['content']
    runtime.complete=complete;runtime.stream_answer=stream
    try:
        await runtime.run('c','t')
        assert store.events[-1][1]['status']=='COMPLETED'
        assert store.trace['recipientProfile']['data']==snapshot()
        assert not hasattr(runtime,'profile')
    finally:await runtime.close()


async def test_inflight_duplicate_cannot_replace_snapshot():
    from app.runtime import Runtime
    class Store:
        async def call(self,method,**args):
            if method=='existing_turn':return None
            if method=='start':return [{'status':'PROCESSING'},True]
            if method=='context':return True
    runtime=Runtime.__new__(Runtime);runtime.init_state(Store(),None,'owner');runtime.model='test'
    entered=asyncio.Event();release=asyncio.Event()
    async def run(cid,tid):entered.set();await release.wait()
    runtime.run=run
    data=snapshot()
    await runtime.accept('c','t','test',recipient_context=data);await entered.wait()
    data['giftPreference']='mutated caller input'
    assert runtime.admissions['t']['recipient_context']['giftPreference']=='차를 좋아합니다.'
    try:
        with pytest.raises(APIError) as err:
            await runtime.accept('c','t','test',recipient_context=snapshot(sourceVersion=9))
        assert err.value.status==409
    finally:release.set();await runtime.stop_jobs()


@pytest.mark.skipif(not os.getenv('TEST_DATABASE_URL'),reason='requires disposable PostgreSQL')
def test_postgres_context_is_atomic_private_and_immutable_across_restarts():
    from app.store import Store
    store=Store(os.environ['TEST_DATABASE_URL']);owner=str(uuid4())
    cid,tid=str(uuid4()),str(uuid4());store.lease(owner)
    try:
        store.conversation(cid,7,42)
        with pytest.raises(APIError) as err:store.start(cid,tid,'test',owner,recipient_context=snapshot(recipientUserId=43))
        assert err.value.status==409 and store.read(cid)['turns']==[]
        state,made=store.start(cid,tid,'test',owner,recipient_context=snapshot())
        assert made and 'recipientContext' not in state
        assert store.recipient_context(cid,tid)==snapshot()
        assert store.start(cid,tid,'test',owner,recipient_context=snapshot())[1] is False
        for changed in [None,snapshot(sourceVersion=9),snapshot(giftPreference='커피')]:
            with pytest.raises(APIError) as err:store.start(cid,tid,'test',owner,recipient_context=changed)
            assert err.value.status==409
        store.emit(cid,tid,'turn.finished',{'status':'COMPLETED'},owner)
        store.close();store=Store(os.environ['TEST_DATABASE_URL'])
        assert store.start(cid,tid,'test',owner,recipient_context=snapshot())[1] is False
        assert store.admin_conversation(cid)['turns'][0]['context']['recipientContext']==snapshot()
        next_tid=str(uuid4());next_context=snapshot(status='NONE',sourceVersion=9,giftPreference=None,dislikedCategories=[])
        store.start(cid,next_tid,'다시 추천',owner,recipient_context=next_context)
        assert store.recipient_context(cid,tid)==snapshot() and store.recipient_context(cid,next_tid)==next_context
        store.emit(cid,next_tid,'turn.finished',{'status':'COMPLETED'},owner)
        omitted=str(uuid4());store.start(cid,omitted,'다시',owner)
        assert store.recipient_context(cid,omitted) is None
    finally:store.close()
