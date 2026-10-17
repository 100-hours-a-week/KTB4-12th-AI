import httpx
from app.errors import APIError
class RemoteStore:
 def __init__(self,url,token):
  self.http=httpx.AsyncClient(base_url=url.rstrip('/'),headers={'Authorization':'Bearer '+token,'ngrok-skip-browser-warning':'1'},timeout=15)
 async def call(self,method,**args):
  try:r=await self.http.post('/rpc',json={'method':method,'args':args})
  except httpx.HTTPError as e:raise APIError(503,'대화 저장소에 연결할 수 없습니다.') from e
  if r.status_code!=200:
   try:message=r.json().get('message','대화 저장소 요청에 실패했습니다.')
   except ValueError:message='대화 저장소 요청에 실패했습니다.'
   raise APIError(r.status_code if r.status_code in {400,401,403,404,409,429} else 503,message)
  return r.json()['result']
 async def close(self):await self.http.aclose()
