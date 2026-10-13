"""Browser session and origin boundaries shared by team UI and admin routes."""
import os,time
from collections import defaultdict,deque
from urllib.parse import urlsplit
from fastapi import Request
from itsdangerous import URLSafeTimedSerializer,BadSignature,SignatureExpired
from app.errors import APIError
SIGNER=URLSafeTimedSerializer(os.environ['SESSION_SECRET'])
SECURE=os.getenv('COOKIE_SECURE','true')=='true'
rate=defaultdict(deque)
def throttle(key,limit,seconds):
 now=time.monotonic();q=rate[key]
 while q and q[0]<now-seconds:q.popleft()
 if len(q)>=limit:raise APIError(429,'요청이 많습니다. 잠시 후 다시 시도해 주세요.')
 q.append(now)
 if len(rate)>5000:
  for k in list(rate)[:1000]:
   if not rate[k] or rate[k][-1]<now-3600:rate.pop(k,None)
def same_origin(request):
 origin=request.headers.get('origin')
 if origin and urlsplit(origin).netloc!=request.headers.get('host'):raise APIError(403,'허용되지 않은 요청 출처입니다.')
 if request.headers.get('sec-fetch-site')=='cross-site':raise APIError(403,'허용되지 않은 요청 출처입니다.')
def admin_session(request:Request):
 same_origin(request)
 try:
  value=SIGNER.loads(request.cookies.get('chat_admin',''),salt='admin',max_age=28800)
  if not isinstance(value,dict) or not valid_token(value.get('version'),admin_access_version()):raise BadSignature('stale admin access')
 except (BadSignature,SignatureExpired):raise APIError(401,'관리자 로그인이 필요합니다.')

def admin_access_version():
 import hmac,hashlib
 # Keyed fingerprint: the cookie must not expose a guessable password hash.
 return hmac.new(os.environ['SESSION_SECRET'].encode(),os.environ['QA_ADMIN_PASSWORD'].encode(),hashlib.sha256).hexdigest()

# An access cookie proves the shared team token was supplied; it is not an API token.
DEMO_ACCESS_SECONDS=8*3600
def valid_token(provided,expected):
 import secrets
 return bool(expected) and isinstance(provided,str) and secrets.compare_digest(provided.encode('utf-8'),expected.encode('utf-8'))
def demo_access_version():
 import hashlib
 return hashlib.sha256(os.environ['DEMO_ACCESS_TOKEN'].encode()).hexdigest()
def has_demo_access(request):
 if not os.getenv('DEMO_ACCESS_TOKEN'):return False
 try:
  value=SIGNER.loads(request.cookies.get('chat_access',''),salt='demo-access',max_age=DEMO_ACCESS_SECONDS)
  return isinstance(value,dict) and valid_token(value.get('version'),demo_access_version())
 except (BadSignature,SignatureExpired):return False
def require_demo_access(request):
 if not has_demo_access(request):raise APIError(401,'체험 토큰을 입력해 주세요.')
