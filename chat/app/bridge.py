"""Authenticated outbound NUC connection carries bounded storage operations."""
import asyncio,secrets,uuid
from fastapi import WebSocket
from app.errors import APIError
class BridgeStore:
 def __init__(self,token):self.token=token;self.socket=None;self.pending={};self.connected=asyncio.Event();self.lock=asyncio.Lock();self.epoch=0
 async def attach(self,ws:WebSocket):
  if not secrets.compare_digest(ws.headers.get('authorization',''),'Bearer '+self.token):await ws.close(code=1008);return
  await ws.accept()
  if self.socket is not None:await ws.close(code=1013);return
  self.socket=ws;self.epoch+=1;self.connected.set()
  try:
   while True:
    data=await ws.receive_json();f=self.pending.pop(data.get('id'),None)
    if f and not f.done():
     if 'error' in data:f.set_exception(APIError(data.get('status',503),data['error']))
     else:f.set_result(data.get('result'))
  except Exception:pass
  finally:
   if self.socket is ws:
    self.socket=None;self.connected.clear()
    for f in self.pending.values():
     if not f.done():f.set_exception(APIError(503,'대화 저장소 연결이 끊겼습니다.'))
    self.pending.clear()
 async def call(self,method,**args):
  if not self.socket:raise APIError(503,'대화 저장소 연결을 준비하고 있습니다.')
  if len(self.pending)>=100:raise APIError(503,'대화 저장 요청이 많습니다.')
  rid=str(uuid.uuid4());future=asyncio.get_running_loop().create_future();self.pending[rid]=future
  try:
   async with self.lock:
    if not self.socket:raise APIError(503,'대화 저장소 연결이 끊겼습니다.')
    await self.socket.send_json({'id':rid,'method':method,'args':args})
   return await asyncio.wait_for(future,20)
  except TimeoutError as e:raise APIError(503,'대화 저장 시간이 초과되었습니다.') from e
  finally:self.pending.pop(rid,None)
 async def close(self):
  if self.socket:await self.socket.close()
