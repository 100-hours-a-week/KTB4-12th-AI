"""Public browser origin through the existing internal Host routing. No real DB/API."""
import os
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from app import main, security
from app.errors import APIError
from app.llm_connection import Connections

PUBLIC='https://www.seonjalal.com'
INTERNAL='v2-origin.seonjalal.com'

@pytest.fixture
def production(monkeypatch):
 monkeypatch.setenv('AI_ENVIRONMENT','production')
 monkeypatch.setattr(security,'SECURE',True)
 security.rate.clear()
 monkeypatch.setattr(main.app.state,'runtime',SimpleNamespace(connections=Connections()),raising=False)


def request(origin=PUBLIC,*,method='POST',host=INTERNAL,extra=()):
 headers=[(b'host',host.encode()),*extra]
 if origin is not None:headers.append((b'origin',origin.encode()))
 return Request({'type':'http','method':method,'path':'/admin/login','scheme':'http','server':('internal',7860),'headers':headers})


def test_production_accepts_public_origin_even_with_internal_http_host(production):
 security.admin_origin(request())
 # This mismatch is the exact old behavior that the administrator path must fix.
 with pytest.raises(APIError):security.same_origin(request())


@pytest.mark.parametrize('origin',[
 'https://seonjalal.com','http://www.seonjalal.com','https://www.seonjalal.com:444',
 'https://www.seonjalal.com.attacker.invalid','https://attacker-www.seonjalal.com',
 'https://attacker.invalid','https://www.seonjalal.com@attacker.invalid',
 'https://user@www.seonjalal.com','https://www.seonjalal.com/path',
 'https://www.seonjalal.com?query=1','null','https://v2-origin.seonjalal.com',
])
def test_only_exact_public_origin_allowed(production,origin):
 with pytest.raises(APIError) as e:security.admin_origin(request(origin))
 assert e.value.status==403


def test_duplicate_origin_and_forwarded_host_cannot_bypass(production):
 for req in [request(extra=[(b'origin',PUBLIC.encode())]),
             request('https://attacker.invalid',extra=[(b'x-forwarded-host',b'www.seonjalal.com'),(b'x-forwarded-proto',b'https')]),
             request(extra=[(b'sec-fetch-site',b'cross-site')])]:
  with pytest.raises(APIError) as e:security.admin_origin(req)
  assert e.value.status==403


@pytest.mark.parametrize('environment',['prod','Production','',' production '])
def test_invalid_environment_fails_closed(monkeypatch,environment):
 monkeypatch.setenv('AI_ENVIRONMENT',environment)
 with pytest.raises(APIError) as e:security.admin_origin(request())
 assert e.value.status==503


@pytest.mark.parametrize('environment',[None,'local','develop','staging'])
def test_nonproduction_keeps_same_host_rule(monkeypatch,environment):
 if environment is None:monkeypatch.delenv('AI_ENVIRONMENT',raising=False)
 else:monkeypatch.setenv('AI_ENVIRONMENT',environment)
 security.admin_origin(request('http://127.0.0.1:18794',host='127.0.0.1:18794'))
 with pytest.raises(APIError):security.admin_origin(request())


def test_missing_origin_only_read_requests_and_secure_cookies_required(production,monkeypatch):
 security.admin_origin(request(None,method='GET'))
 for method in ['POST','PUT','PATCH','DELETE']:
  with pytest.raises(APIError) as e:security.admin_origin(request(None,method=method))
  assert e.value.status==403
 monkeypatch.setattr(security,'SECURE',False)
 with pytest.raises(APIError) as e:security.admin_origin(request())
 assert e.value.status==503


async def proxy(scope,receive,send):
 # Browser sees public HTTPS, while the Chat upstream receives internal Host/HTTP.
 if scope['type']=='http':
  scope={**scope,'scheme':'http','headers':[(k,v) for k,v in scope['headers'] if k.lower()!=b'host']+[(b'host',INTERNAL.encode())]}
 await main.app(scope,receive,send)


async def test_signed_cookie_activation_and_logout_through_proxy(production):
 async with httpx.AsyncClient(base_url=PUBLIC,transport=httpx.ASGITransport(app=proxy)) as client:
  path='/admin/api/model-connection'
  assert (await client.get(path)).status_code==401
  wrong=await client.post('/admin/login',json={'password':'synthetic-wrong'},headers={'Origin':PUBLIC})
  assert wrong.status_code==401
  login=await client.post('/admin/login',json={'password':os.environ['QA_ADMIN_PASSWORD']},headers={'Origin':PUBLIC})
  assert login.status_code==200
  cookie=login.headers['set-cookie']
  assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie and 'Domain=' not in cookie
  assert 'chat_admin' in client.cookies
  state=await client.get(path);assert state.status_code==200
  body={'revision':state.json()['revision'],'key':'synthetic-proxy-inference-key','config':{'kind':'openai-compatible','baseUrl':'https://model.example/v1','model':'synthetic'}}
  denied=await client.post(path+'/activate',json=body,headers={'Origin':'https://attacker.invalid'})
  assert denied.status_code==403
  active=await client.post(path+'/activate',json=body,headers={'Origin':PUBLIC})
  assert active.status_code==200 and active.json()['endpointActive']
  assert body['key'] not in active.text
  off=await client.post(path+'/modal-off',json={'revision':active.json()['revision']},headers={'Origin':PUBLIC})
  assert off.status_code==200 and not off.json()['endpointActive']
  out=await client.post('/admin/logout',headers={'Origin':PUBLIC});assert out.status_code==200
  assert (await client.get(path)).status_code==401


async def test_session_invalidated_password_rotation_and_missing_origin(production,monkeypatch):
 async with httpx.AsyncClient(base_url=PUBLIC,transport=httpx.ASGITransport(app=proxy)) as client:
  login='/admin/login';data={'password':os.environ['QA_ADMIN_PASSWORD']}
  assert (await client.post(login,json=data)).status_code==403
  assert (await client.post(login,json=data,headers={'Origin':PUBLIC})).status_code==200
  monkeypatch.setenv('QA_ADMIN_PASSWORD','synthetic-password-rotated')
  assert (await client.get('/admin/api/model-connection')).status_code==401
