"""Narrow authenticated storage RPC; PostgreSQL stays on the private Docker network."""
import os,secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
from app.store import Store
from app.errors import APIError,install_error_handlers
from pydantic import BaseModel,ConfigDict
TOKEN=os.environ['STORE_TOKEN']
METHODS={'review_report','report_summary','conversation_feedback','admin_reports','own_feedback','budget_summary','budget_reserve','budget_finish','health','lease','conversation','read','existing_turn','start','emit','replay','context','session','owned','list_conversations','feedback','admin','binding','admin_conversations','admin_conversation','recipient_context'}
@asynccontextmanager
async def lifespan(app):
 app.state.store=Store(os.environ['DATABASE_URL']);yield;app.state.store.close()
app=FastAPI(lifespan=lifespan,docs_url=None,openapi_url=None)
install_error_handlers(app)
class Call(BaseModel):
 model_config=ConfigDict(extra='forbid')
 method:str
 args:dict={}
@app.middleware('http')
async def authenticate(request,call_next):
 if not secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+TOKEN):return JSONResponse({'error':'unauthorized'},status_code=401)
 if int(request.headers.get('content-length','0'))>2_000_000:return JSONResponse({'error':'too large'},status_code=413)
 return await call_next(request)
@app.post('/rpc')
def rpc(body:Call):
 if body.method not in METHODS:raise APIError(400,'Unknown operation')
 return {'result':getattr(app.state.store,body.method)(**body.args)}
