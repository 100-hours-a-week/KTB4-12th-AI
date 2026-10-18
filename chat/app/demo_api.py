"""Shared trial code authenticates entry; signed sessions still own every conversation."""
import os
from typing import Annotated
from fastapi import APIRouter, Request, Response, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from itsdangerous import BadSignature, SignatureExpired
from pydantic import UUID4, Field, field_validator
from app.models import PutConversation, StrictModel, PutTurn
from app.security import SIGNER, same_origin, valid_token, throttle
from app.errors import APIError

router=APIRouter(include_in_schema=False)
bearer=HTTPBearer(auto_error=False)
async def trial_owner(request:Request,credentials:Annotated[HTTPAuthorizationCredentials|None,Depends(bearer)]):
    same_origin(request)
    if credentials is None or not valid_token(credentials.credentials,os.getenv('DEMO_ACCESS_TOKEN','')):
        throttle(('demo-api-login',request.client.host),10,300)
        raise APIError(401,'체험 코드가 필요합니다.')
    try:sid=SIGNER.loads(request.cookies.get('chat_session',''),salt='demo',max_age=86400*30)
    except (BadSignature,SignatureExpired):raise APIError(401,'먼저 /demo/session에서 체험 코드를 입력하고 쿠키를 유지해 주세요.')
    return await request.app.state.store.call('session',sid=sid)
Owner=Annotated[int,Depends(trial_owner)]
LIVE='/demo/api/conversations';FREE='/demo/api/test/conversations'
async def owned(request,cid,sender):
    store=request.app.state.store
    await store.call('owned',cid=cid,sender=sender)
    binding=await store.call('binding',cid=cid)
    fixture=request.url.path.startswith(FREE)
    if bool(binding.get('protocolFixture'))!=fixture:raise APIError(409,'대화를 시작한 실행 경로를 사용해 주세요.')
    return fixture

@router.put(LIVE+'/{cid}')
@router.put(FREE+'/{cid}')
async def create(cid:UUID4,body:PutConversation,request:Request,response:Response,sender:Owner):
    if body.sender_id!=sender:raise APIError(403,'접속 세션의 senderUserId를 사용해 주세요.')
    store=request.app.state.store;ident=str(cid)
    # Check existing ownership before restoring or conflicting with its binding.
    try:await store.call('binding',cid=ident)
    except APIError as exc:
        if exc.status!=404:raise
    else:await store.call('owned',cid=ident,sender=sender)
    data,made=await store.call('conversation',cid=ident,sender=sender,recipient=body.recipient_id,protocol_fixture=request.url.path.startswith(FREE))
    response.status_code=201 if made else 200
    return {'message':'대화를 조회했습니다.','data':data}

@router.get(LIVE+'/{cid}')
@router.get(FREE+'/{cid}')
async def conversation(cid:UUID4,request:Request,sender:Owner):
    await owned(request,str(cid),sender)
    return {'message':'대화를 조회했습니다.','data':await request.app.state.store.call('read',cid=str(cid))}

@router.get(LIVE+'/{cid}/turns/{tid}')
@router.get(FREE+'/{cid}/turns/{tid}')
async def snapshot(cid:UUID4,tid:UUID4,request:Request,sender:Owner):
    await owned(request,str(cid),sender)
    _,state=await request.app.state.store.call('replay',cid=str(cid),tid=str(tid),after=0)
    return {'message':'질문 상태를 조회했습니다.','data':state}

class TrialTurn(StrictModel):
    message:Annotated[str,Field(min_length=1,max_length=200)]
    @field_validator("message",mode="before")
    @classmethod
    def trim(cls,value):return value.strip() if isinstance(value,str) else value
@router.put(LIVE+'/{cid}/turns/{tid}')
@router.put(FREE+'/{cid}/turns/{tid}')
async def turn(cid:UUID4,tid:UUID4,body:TrialTurn,request:Request,sender:Owner):
    from app.main import proxied,BASE,TEST_BASE
    fixture=await owned(request,str(cid),sender);throttle(('turns',sender),30,3600)
    # Use the deployed admission/budget/fixture implementation unchanged.
    return await proxied(f'{TEST_BASE if fixture else BASE}/{cid}/turns/{tid}',body.model_dump())

@router.get(LIVE+'/{cid}/turns/{tid}/events')
@router.get(FREE+'/{cid}/turns/{tid}/events')
async def events(cid:UUID4,tid:UUID4,request:Request,sender:Owner,afterEventId:Annotated[int,Query(ge=0)]=0):
    from app.main import proxied,BASE,TEST_BASE
    if set(request.query_params)-{'afterEventId'}:raise APIError(400,'지원하지 않는 Query 필드입니다.')
    fixture=await owned(request,str(cid),sender)
    return await proxied(f'{TEST_BASE if fixture else BASE}/{cid}/turns/{tid}/events?afterEventId={afterEventId}',stream=True)

@router.put(LIVE+'/{cid}/turns/{tid}/cancellation')
@router.put(FREE+'/{cid}/turns/{tid}/cancellation')
async def cancel(cid:UUID4,tid:UUID4,request:Request,sender:Owner):
    from app.main import proxied,BASE,TEST_BASE
    fixture=await owned(request,str(cid),sender)
    return await proxied(f'{TEST_BASE if fixture else BASE}/{cid}/turns/{tid}/cancellation')
