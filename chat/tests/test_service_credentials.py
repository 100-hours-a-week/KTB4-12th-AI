"""Managed BE tokens keep the existing routes/envelopes and reject before storage."""
import asyncio
import secrets
import uuid
from types import SimpleNamespace

import httpx
import pytest

from app import main
from integration.service_token import ServiceToken


async def test_chat_service_rotation_preserves_accepted_request_and_blocks_old(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_SERVICE_TOKEN")
    monkeypatch.setenv("BE_SERVICE_TOKEN_DIR", str(tmp_path))
    source=ServiceToken.from_environment("chat")
    old,new=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
    entered,release=asyncio.Event(),asyncio.Event()
    calls=[]
    async def call(method, **kwargs):
        calls.append(method);entered.set();await release.wait()
        # Intentional 503 after admission: verifies auth precedes any storage,
        # without manufacturing a successful DB response or touching a DB.
        raise main.APIError(503,"synthetic storage unavailable")
    monkeypatch.setattr(main.app.state,"store",SimpleNamespace(call=call),raising=False)
    path=main.BASE+"/"+str(uuid.uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(main.app),base_url="https://test") as client:
        body={"senderUserId":7,"recipientUserId":42}
        r=await client.put(path,json=body,headers={"Authorization":"Bearer "+old})
        assert r.status_code==503 and not calls
        saved=source.save(old,None)
        accepted=asyncio.create_task(client.put(path,json=body,headers={"Authorization":"Bearer "+old}))
        await asyncio.wait_for(entered.wait(),2)
        source.save(new,saved["revision"])
        r=await client.put(path,json=body,headers={"Authorization":"Bearer "+old})
        assert r.status_code==401 and calls==["conversation"]
        release.set();r=await accepted
        assert r.status_code==503 and r.json()["error"]["code"]=="SERVICE_UNAVAILABLE"
        r=await client.put(path,json=body,headers={"Authorization":"Bearer "+new})
        assert r.status_code==503 and calls==["conversation","conversation"]
        assert old not in r.text and new not in r.text
        monkeypatch.setenv("AI_SERVICE_TOKEN", "conflicting-legacy")
        r=await client.put(path,json=body,headers={"Authorization":"Bearer "+new})
        assert r.status_code==503 and len(calls)==2


async def test_internal_proxy_auth_reads_new_token_for_each_request(tmp_path,monkeypatch):
    monkeypatch.delenv("AI_SERVICE_TOKEN")
    monkeypatch.setenv("BE_SERVICE_TOKEN_DIR",str(tmp_path))
    source=ServiceToken.from_environment("chat")
    first,second=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
    seen=[]
    def respond(request):
        seen.append(request.headers["Authorization"]);return httpx.Response(200,json={})
    async with httpx.AsyncClient(auth=main.LocalServiceAuth(),transport=httpx.MockTransport(respond)) as client:
        saved=source.save(first,None);await client.get("https://test/one")
        source.save(second,saved["revision"]);await client.get("https://test/two")
    assert seen==["Bearer "+first,"Bearer "+second]
