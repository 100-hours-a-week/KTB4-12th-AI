"""AI quality workspace: authenticated conversation and feedback inspection."""
import os,secrets
from datetime import datetime
from typing import Annotated,Literal
from pathlib import Path
from fastapi import APIRouter,Depends,Request,Response,Query
from fastapi.responses import FileResponse
from pydantic import BaseModel,Field,UUID4
from app.errors import APIError
from app.review_models import ReportReview,TurnReview
from app.release import revision as app_revision
from app.security import SIGNER,SECURE,admin_session,admin_access_version,same_origin,throttle,valid_token
router=APIRouter(include_in_schema=False)
Admin=Annotated[None,Depends(admin_session)]
class Login(BaseModel):password:str=Field(max_length=200)
class Review(BaseModel):reviewed:bool
@router.get('/admin/qa')
def page():return FileResponse(Path(__file__).parent/'static'/'admin.html')
@router.post('/admin/login')
async def login(body:Login,request:Request,response:Response):
 same_origin(request);throttle(('admin',request.client.host),8,300)
 if not valid_token(body.password,os.environ['QA_ADMIN_PASSWORD']):raise APIError(401,'비밀번호를 확인해 주세요.')
 response.set_cookie('chat_admin',SIGNER.dumps({'version':admin_access_version()},salt='admin'),max_age=28800,secure=SECURE,httponly=True,samesite='lax')
 return {'ok':True}
@router.post('/admin/logout')
def logout(request:Request,response:Response,auth:Admin):
 response.delete_cookie('chat_admin',secure=SECURE,httponly=True,samesite='lax');return {'ok':True}
@router.get('/admin/api/conversations')
async def conversations(request:Request,auth:Admin,limit:Annotated[int,Query(ge=1,le=50)]=20,cursor:Annotated[str|None,Query(max_length=300)]=None,status:Literal['PROCESSING','COMPLETED','FAILED','CANCELLED']|None=None,since:datetime|None=None,until:datetime|None=None,q:Annotated[str,Query(max_length=200)]='',feedback:Literal['all','any','pending','reviewed']='all',sort:Literal['created','feedback']='created'):
 if any(d is not None and d.tzinfo is None for d in [since,until]):raise APIError(400,'날짜에 시간대가 필요합니다.')
 if since and until and since>=until:raise APIError(400,'종료일은 시작일 이후여야 합니다.')
 data=await request.app.state.store.call('admin_conversations',limit=limit,cursor=cursor,status=status,since=since.isoformat() if since else None,until=until.isoformat() if until else None,q=q,feedback_state=feedback,sort=sort)
 for row in data.get('items',[]):row['title']='[요청 원문 숨김]'
 return data
@router.get('/admin/api/conversations/{cid}')
async def conversation(cid:UUID4,request:Request,auth:Admin,after:Annotated[int,Query(ge=0)]=0,limit:Annotated[int,Query(ge=1,le=50)]=20,focus:Annotated[UUID4|None,Query(alias='focusTurnId')]=None):
 if focus and after:raise APIError(400,'의견 위치 이동과 다음 페이지 조회는 함께 사용할 수 없습니다.')
 from app.request_audit import conversation_projection
 data=await request.app.state.store.call('admin_conversation',cid=str(cid),after=after,limit=limit,focus_tid=str(focus) if focus else None)
 return conversation_projection(data)
@router.put('/admin/api/conversations/{cid}/turns/{tid}/reviews/{review_id}')
async def turn_review(cid:UUID4,tid:UUID4,review_id:UUID4,body:TurnReview,request:Request,auth:Admin):
 return await request.app.state.store.call('admin_turn_review',cid=str(cid),tid=str(tid),review_id=str(review_id),note=body.note)
@router.get('/admin/api/feedback')
async def feedback(request:Request,auth:Admin):return await request.app.state.store.call('admin')
@router.put('/admin/api/feedback/{fid}')
async def review(fid:UUID4,body:Review,request:Request,auth:Admin):return await request.app.state.store.call('admin',fid=str(fid),reviewed=body.reviewed)

@router.get('/admin/reports')
def report_page():return FileResponse(Path(__file__).parent/'static'/'reports-admin.html')
@router.get('/admin/api/reports')
async def report_list(request:Request,auth:Admin,limit:Annotated[int,Query(ge=1,le=100)]=50,cursor:Annotated[str|None,Query(max_length=400)]=None,status:Literal['all','unresolved','pending','reviewed','triaged','resolved']='all'):
 return await request.app.state.store.call('admin_reports',limit=limit,cursor=cursor,status=status)
@router.get('/admin/api/reports/{fid}')
async def report_detail(fid:UUID4,request:Request,auth:Admin):return await request.app.state.store.call('admin_reports',fid=str(fid))

@router.put('/admin/api/reports/{fid}/review')
async def report_workflow(fid:UUID4,body:ReportReview,request:Request,auth:Admin):
 if body.evidence is not None and body.evidence.revision!=app_revision():raise APIError(409,'해결 근거의 revision과 현재 배포가 다릅니다.')
 return await request.app.state.store.call('review_report',fid=str(fid),review_id=str(body.review_id),status=body.status,note=body.note,evidence=body.evidence.model_dump(mode='json',by_alias=True) if body.evidence else None)

@router.put('/admin/api/reports/{fid}')
async def report_review(fid:UUID4,body:Review,request:Request,auth:Admin):
 return await request.app.state.store.call('admin_reports',fid=str(fid),reviewed=body.reviewed)

@router.get('/admin/api/report-summary')
async def report_summary(request:Request,auth:Admin,limit:Annotated[int,Query(ge=1,le=100)]=50,cursor:Annotated[str|None,Query(max_length=400)]=None,status:Literal['all','unresolved','pending','reviewed','triaged','resolved']='unresolved'):
 return await request.app.state.store.call('report_summary',limit=limit,cursor=cursor,status=status)

class ConnectionChange(BaseModel):
 model_config={'extra':'forbid'}
 revision:int=Field(ge=0)
 config:dict
class ConnectionProbe(BaseModel):
 model_config={'extra':'forbid'}
 config:dict
@router.get('/admin/api/model-connection')
async def connection_status(request:Request,auth:Admin):
 connections=getattr(request.app.state.runtime,'connections',None)
 if connections is None:raise APIError(409,'현재 실행기는 모델 연결 설정을 지원하지 않습니다.')
 return connections.public()
@router.put('/admin/api/model-connection')
async def connection_save(body:ConnectionChange,request:Request,auth:Admin):
 return await request.app.state.runtime.connections.publish(body.config,body.revision)
@router.post('/admin/api/model-connection/check')
async def connection_probe(body:ConnectionProbe,request:Request,auth:Admin):
 throttle(('connection-probe',request.client.host),3,300)
 return await request.app.state.runtime.connections.check(body.config)
@router.post('/admin/api/model-connection/rollback')
async def connection_rollback(body:ConnectionChange,request:Request,auth:Admin):
 return await request.app.state.runtime.connections.rollback(body.revision)
@router.get('/admin/api/conversations/{cid}/turns/{tid}/trace')
async def request_trace(cid:UUID4,tid:UUID4,request:Request,auth:Admin,details:bool=False):
 from app.request_audit import trace
 # Existing administrator scope, exact conversation/turn ownership checked by store.
 data=await request.app.state.store.call('admin_conversation',cid=str(cid),limit=1,focus_tid=str(tid))
 turn=next((t for t in data['turns'] if t['turnId']==str(tid)),None)
 if turn is None:raise APIError(404,'해당 대화의 질문을 찾을 수 없습니다.')
 result=trace(str(cid),turn['state'],turn.get('context') or {},details=details,created=turn.get('created'))
 result['sseEvents']=turn.get('eventSummary',{'events':[],'basis':'not-recorded-in-this-projection','truncated':None})
 return result
