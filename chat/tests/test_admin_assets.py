"""Admin pages need only /admin/* routing; shared public assets stay compatible."""
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest

from app import main, security

ASSETS={'admin.css':'text/css','http.js':'text/javascript','admin.js':'text/javascript',
        'guide.css':'text/css','reports-admin.js':'text/javascript'}


@pytest.mark.parametrize('name,media_type',ASSETS.items())
async def test_public_admin_assets_and_existing_static_are_identical(name,media_type):
 async with httpx.AsyncClient(base_url='http://test',transport=httpx.ASGITransport(app=main.app)) as client:
  new=await client.get('/admin/static/'+name)
  old=await client.get('/static/'+name)
  head=await client.head('/admin/static/'+name)
  assert new.status_code==old.status_code==head.status_code==200
  assert new.content==old.content==(Path(main.STATIC)/name).read_bytes()
  assert new.headers['content-type'].startswith(media_type)
  assert head.headers['content-type']==new.headers['content-type']
  assert head.headers['content-length']==new.headers['content-length']
  assert not head.content
  assert new.headers['cache-control']=='no-store, no-transform'
  assert new.headers['x-content-type-options']=='nosniff'
  assert 'set-cookie' not in new.headers


@pytest.mark.parametrize('path',[
 '/admin/static/missing.js','/admin/static/admin.html','/admin/static/index.html',
 '/admin/static/admin.py','/admin/static/../admin.py',
 '/admin/static/%2e%2e%2fadmin.py','/admin/static/%2e%2e%5cadmin.py',
 '/admin/static/%252e%252e%252fadmin.py','/admin/static/admin.js/extra',
])
async def test_unknown_and_traversal_paths_are_not_served(path):
 async with httpx.AsyncClient(base_url='http://test',transport=httpx.ASGITransport(app=main.app)) as client:
  assert (await client.get(path)).status_code==404


class AssetReferences(HTMLParser):
 def __init__(self):
  super().__init__();self.paths=[]
 def handle_starttag(self,tag,attrs):
  for name,value in attrs:
   if (tag,name) in {('script','src'),('link','href')}:self.paths.append(value)


@pytest.mark.parametrize('page,names',[
 ('/admin/qa',{'admin.css','http.js','admin.js'}),
 ('/admin/reports',{'guide.css','reports-admin.js'}),
])
async def test_both_pages_load_assets_only_under_admin_without_login(page,names):
 async with httpx.AsyncClient(base_url='http://test',transport=httpx.ASGITransport(app=main.app)) as client:
  response=await client.get(page)
  assert response.status_code==200
  refs=AssetReferences();refs.feed(response.text)
  assert set(refs.paths)=={'/admin/static/'+name for name in names}
  for path in refs.paths:assert (await client.get(path)).status_code==200
  assert 'script-src \'self\'' in response.headers['content-security-policy']


async def test_asset_routing_does_not_change_login_or_data_authentication(monkeypatch):
 monkeypatch.setenv('AI_ENVIRONMENT','local')
 security.rate.clear()
 async with httpx.AsyncClient(base_url='https://test',transport=httpx.ASGITransport(app=main.app)) as client:
  paths=['/admin/api/conversations','/admin/api/reports','/admin/api/model-connection']
  for path in paths:assert (await client.get(path)).status_code==401
  assert (await client.get('/admin/static/admin.js')).status_code==200
  login=await client.post('/admin/login',json={'password':'local-test-admin'},headers={'Origin':'https://test'})
  assert login.status_code==200
  assert 'httponly' in login.headers['set-cookie'].lower()
  assert (await client.post('/admin/logout',headers={'Origin':'https://test'})).status_code==200
  for path in paths:assert (await client.get(path)).status_code==401
  assert (await client.post('/admin/static/admin.js')).status_code==405
