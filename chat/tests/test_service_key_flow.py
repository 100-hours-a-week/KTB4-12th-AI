"""Exercise BE API → real Chat harness → synthetic OpenRouter/search/store."""
import asyncio
import json
import os
import uuid

import httpx
import pytest
from starlette.datastructures import State

from app import main
from app.low_latency import LowLatencyRuntime
from app.model_config import ModelCatalog
from integration.provider_key import ProviderKey
from integration.service_token import ServiceToken
from test_low_latency import Store, Search, text, tool, context, show


@pytest.mark.parametrize("provider_status", [200, 402, 429])
async def test_live_api_provider_limit_mode_rotation_and_no_other_service_key(tmp_path, monkeypatch, provider_status, caplog):
    for name in ("OPENROUTER_API_KEY", "OPENROUTER_API_KEY_FILE", "PROFILING_OPENROUTER_API_KEY",
                 "AI_SERVICE_TOKEN", "PROFILING_SERVICE_TOKEN", "CHAT_BE_SERVICE_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_MANAGED_KEY_DIR", str(tmp_path))
    monkeypatch.setenv("BE_SERVICE_TOKEN_DIR", str(tmp_path))
    source = ProviderKey.from_environment("chat")
    old, new, other = ("sk-or-v1-" + name * 8 for name in ("synthetic_old_", "synthetic_new_", "synthetic_other_"))
    saved = source.save(old, None)
    ProviderKey.from_environment("profiler").save(other, None)
    token = "synthetic_common_BE_token_" * 2
    ServiceToken.from_environment("chat").save(token, None)
    store, search, wire = Store(), Search(), []
    runtime = LowLatencyRuntime(store, search, "synthetic-owner")
    auth = runtime.http._auth
    await runtime.http.aclose()
    packets = [tool("gift_context", context(budget=20000)), tool("show_gifts", show()), text("안녕하세요.")]
    async def provider(request):
        assert request.url == "https://openrouter.ai/api/v1/chat/completions"
        wire.append(request.headers["authorization"])
        if provider_status != 200:
            return httpx.Response(provider_status, text=other)
        packet = packets.pop(0)
        if len(wire) == 1: source.save(new, saved["revision"])
        return httpx.Response(200, text="data: " + json.dumps(packet, ensure_ascii=False) + "\n\ndata: [DONE]\n\n")
    runtime.http = httpx.AsyncClient(base_url="https://openrouter.ai/api/v1", auth=auth,
                                    transport=httpx.MockTransport(provider),
                                    event_hooks={"request": [main.provider_key_request]}, follow_redirects=False)
    runtime.catalog = ModelCatalog(runtime.http)
    monkeypatch.setattr(main.app, "state", State({"store": store, "runtime": runtime,
                                               "runtime_mode": "live", "budget_mode": "provider-key-limits"}))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(main.app), base_url="https://test",
                                     headers={"Authorization": "Bearer " + token}) as client:
            observed = (await client.get("/internal/credentials")).json()
            assert observed["service"] == "chat" and observed["provider"]["revision"] == saved["revision"]
            cid = str(uuid.uuid4()); root = main.BASE + "/" + cid
            assert (await client.put(root, json={"senderUserId": 7, "recipientUserId": 42})).status_code == 201
            for index in range(2 if provider_status == 200 else 1):
                tid = str(uuid.uuid4()); path = root + "/turns/" + tid
                response = await client.put(path, json={"message": "합성 선물 요청" if not index else "안녕"})
                assert response.status_code == 202, response.text
                await asyncio.gather(*list(runtime.jobs.values()))
                final = (await client.get(path)).json()["data"]
                assert final["status"] == ("COMPLETED" if provider_status == 200 else "FAILED")
                replay = await client.get(path + "/events")
                assert "turn.finished" in replay.text
                assert all(value not in response.text + replay.text + caplog.text for value in (old, new, other, token))
            if provider_status == 200:
                assert wire == ["Bearer " + old, "Bearer " + old, "Bearer " + new]
                assert search.queries and store.states[next(iter(store.states))]["blocks"]
            else:
                assert wire and all(value == "Bearer " + old for value in wire)
            assert store.budget == []
            (tmp_path / source.record_file).unlink()
            assert (await client.put(root + "/turns/" + str(uuid.uuid4()), json={"message": "missing key"})).status_code == 503
    finally:
        await runtime.stop_jobs()
        await runtime.http.aclose()


@pytest.mark.parametrize("injected", [False, True])
async def test_local_admin_missing_or_injected_key_rotation_and_restart(tmp_path, monkeypatch, injected):
    """Actual admin/Chat HTTP routes and auth; only provider/search/DB are fake."""
    from console.server import create_app
    from console.tests.auth_support import fixture
    for name in list(os.environ):
        if name.startswith(("AI_", "OPENROUTER_", "PROFILING_", "BE_", "CONSOLE_", "CHAT_BE_", "PROFILER_BE_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AI_ENVIRONMENT", "local")
    monkeypatch.setenv("OPENROUTER_MANAGED_KEY_DIR", str(tmp_path))
    monkeypatch.setenv("BE_SERVICE_TOKEN_DIR", str(tmp_path))
    token = "synthetic_common_BE_" * 3
    monkeypatch.setenv("BE_SERVICE_TOKEN", token)
    old, new = ("sk-or-v1-" + label * 5 for label in ("synthetic_initial_", "synthetic_updated_"))
    if injected: monkeypatch.setenv("OPENROUTER_API_KEY", old)
    hash_file, password = fixture()
    config = {"liveModel": True, "budgetMode": "provider-key-limits",
              "targets": {"chat": "http://127.0.0.1:7860"},
              "adminAuth": {"passwordHashFile": str(hash_file), "sessionFile": str(tmp_path / "sessions.sqlite3")}}

    class ReadyStore(Store):
        async def call(self, method, **kwargs):
            return {} if method == "health" else await super().call(method, **kwargs)

    class ReadySearch(Search):
        async def ready(self): pass

    store, search, wire = ReadyStore(), ReadySearch(), []
    entered, release = asyncio.Event(), asyncio.Event()
    packets = [tool("gift_context", context()), tool("show_gifts", show()), text("안녕하세요.")]

    async def provider(request):
        assert request.method == "POST" and request.url.path == "/api/v1/chat/completions"
        wire.append(request.headers["authorization"])
        if len(wire) == 1:
            entered.set()
            await asyncio.wait_for(release.wait(), 3)
        return httpx.Response(200, text="data: " + json.dumps(packets.pop(0), ensure_ascii=False) + "\n\ndata: [DONE]\n\n")

    async def make_runtime():
        value = LowLatencyRuntime(store, search, "synthetic-owner")
        auth = value.http._auth
        await value.http.aclose()
        value.http = httpx.AsyncClient(base_url="https://openrouter.ai/api/v1", auth=auth,
                                      transport=httpx.MockTransport(provider),
                                      event_hooks={"request": [main.provider_key_request]})
        value.catalog = ModelCatalog(value.http)
        monkeypatch.setattr(main.app, "state", State({"store": store, "search": search, "runtime": value,
            "runtime_mode": "live", "budget_mode": "provider-key-limits", "budget_store": None}))
        return value

    async def make_console():
        value = create_app(config)
        await value.state.backend.http.aclose()
        value.state.backend.http = httpx.AsyncClient(transport=httpx.ASGITransport(main.app))
        return value

    runtime = await make_runtime()
    admin = await make_console()
    source = ProviderKey.from_environment("chat")
    endpoint = "/api/admin/provider-key/chat"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(admin), base_url="http://127.0.0.1:9000",
                                     headers={"Origin": "http://127.0.0.1:9000"}) as browser, \
                   httpx.AsyncClient(transport=httpx.ASGITransport(main.app), base_url="http://127.0.0.1:7860",
                                     headers={"Authorization": "Bearer " + token}) as chat:
            assert (await browser.get(endpoint)).status_code == 401
            login = await browser.post("/api/admin/login", json={"password": password})
            assert login.status_code == 200
            browser.headers["X-CSRF-Token"] = login.json()["csrf"]
            assert (await browser.get("/")).status_code == 200
            initial = (await browser.get(endpoint)).json()
            assert initial["writable"] and initial["configured"] is injected
            assert (await browser.get("/api/config")).json()["chatSendAllowed"] is injected
            assert wire == []
            if not injected:
                assert (await chat.get("/readyz")).status_code == 503
                saved = await browser.put(endpoint, json={"key": old, "revision": None})
                assert saved.status_code == 200
            assert (await browser.get("/api/config")).json()["chatSendAllowed"] is True
            revision = (await browser.get(endpoint)).json()["revision"]
            cid = str(uuid.uuid4()); root = main.BASE + "/" + cid
            assert (await chat.put(root, json={"senderUserId": 7, "recipientUserId": 42})).status_code == 201
            first = root + "/turns/" + str(uuid.uuid4())
            assert (await chat.put(first, json={"message": "합성 선물 요청"})).status_code == 202
            await asyncio.wait_for(entered.wait(), 3)
            saved = await browser.put(endpoint, json={"key": new, "revision": revision})
            assert saved.status_code == 200 and saved.json()["application"] == {"chat": "unverified"}
            observed = (await browser.get(endpoint)).json()
            assert observed["application"] == {"chat": "observed"}
            release.set()
            await asyncio.gather(*list(runtime.jobs.values()))
            assert (await chat.get(first)).json()["data"]["status"] == "COMPLETED"
            assert wire == ["Bearer " + old, "Bearer " + old]
            for invalid in ("bad", "Bearer " + old, {"invalid": old}):
                rejected = await browser.put(endpoint, json={"key": invalid, "revision": observed["revision"]})
                assert rejected.status_code == 400 and old not in rejected.text
                assert source.snapshot().value == new and source.snapshot().revision == observed["revision"]
            assert (await browser.put(endpoint, json={"key": old, "revision": None})).status_code == 409

            # Recreate both processes with the same persistent directory. Even
            # a newly injected initial value cannot replace a dashboard save.
            await runtime.stop_jobs(); await runtime.http.aclose()
            monkeypatch.setenv("OPENROUTER_API_KEY", old)
            runtime = await make_runtime()
            restarted = await make_console()
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(restarted), base_url="http://127.0.0.1:9000",
                                             cookies=browser.cookies) as again:
                    assert (await again.get("/api/admin/session")).status_code == 200
                    assert (await again.get(endpoint)).json()["revision"] == observed["revision"]
                second = root + "/turns/" + str(uuid.uuid4())
                assert (await chat.put(second, json={"message": "안녕"})).status_code == 202
                await asyncio.gather(*list(runtime.jobs.values()))
                assert (await chat.get(second)).json()["data"]["status"] == "COMPLETED"
                assert wire == ["Bearer " + old, "Bearer " + old, "Bearer " + new]
            finally:
                await restarted.state.backend.http.aclose(); await restarted.state.provider_usage.close()

            # Missing Console authentication never enables setup and never
            # participates in the independently authenticated Chat readiness.
            locked = create_app({k: v for k, v in config.items() if k != "adminAuth"})
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(locked), base_url="http://127.0.0.1:9000") as missing:
                    assert (await missing.get("/healthz")).json()["adminConfigured"] is False
                    assert (await missing.get(endpoint)).status_code == 503
                assert (await chat.get("/readyz")).status_code == 200
            finally:
                await locked.state.backend.http.aclose(); await locked.state.provider_usage.close()
    finally:
        release.set()
        await runtime.stop_jobs(); await runtime.http.aclose()
        await admin.state.backend.http.aclose(); await admin.state.provider_usage.close()
