"""NUC worker: outbound WSS only. Postgres is never exposed to the internet."""
import asyncio,json,os,logging
from websockets.asyncio.client import connect
from app.store import Store
from app.gateway import METHODS
from app.errors import APIError
async def main():
 store=Store(os.environ['DATABASE_URL']);limit=asyncio.Semaphore(6)
 while True:
  try:
   async with connect(os.environ['CHAT_BRIDGE_URL'],additional_headers={'Authorization':'Bearer '+os.environ['STORE_TOKEN']},max_size=2_000_000,ping_interval=20,ping_timeout=20) as socket:
    logging.warning('Chat storage bridge connected')
    async def execute(data):
     async with limit:
      try:
       if data['method'] not in METHODS:raise APIError(400,'Unknown operation')
       result=await asyncio.to_thread(getattr(store,data['method']),**data['args'])
       reply={'id':data['id'],'result':result}
      except APIError as e:reply={'id':data['id'],'status':e.status,'error':e.message}
      except Exception as e:
       logging.warning('Storage operation failed: %s',type(e).__name__)
       reply={'id':data['id'],'status':503,'error':'대화 저장 요청에 실패했습니다.'}
      await socket.send(json.dumps(reply,ensure_ascii=False))
    async with asyncio.TaskGroup() as group:
     async for raw in socket:
      group.create_task(execute(json.loads(raw)))
  except Exception as e:
   logging.warning('Storage bridge reconnecting: %s',type(e).__name__);await asyncio.sleep(3)
asyncio.run(main())
