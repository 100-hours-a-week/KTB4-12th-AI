"""Authenticated admin writes and independently observed runtime revisions."""
import asyncio
import json
import os

import httpx
import pytest

from console.server import create_app
from console.tests.auth_support import fixture
from integration.provider_key import ProviderKey
from integration.service_token import ServiceToken


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("AI_", "OPENROUTER_", "PROFILING_", "BE_", "CHAT_BE_", "PROFILER_BE_", "CONSOLE_ADMIN_")):
            monkeypatch.delenv(name)


def test_saved_and_runtime_observed_are_distinct_environment_scoped_and_write_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ENVIRONMENT", "develop")
    monkeypatch.setenv("OPENROUTER_MANAGED_KEY_DIR", str(tmp_path))
    monkeypatch.setenv("BE_SERVICE_TOKEN_DIR", str(tmp_path))
    sources = {kind: ProviderKey.from_environment(kind) for kind in ("chat", "profiler")}
    common = "synthetic_common_BE_token_" * 2
    ServiceToken.from_environment("chat").save(common, None)
    path, password = fixture()
    app = create_app({"adminAuth": {"passwordHashFile": str(path)}, "targets": {"chat": "http://127.0.0.1:7860", "profiling": "http://127.0.0.1:8001"}})
    state = {"environment": "develop", "offline": False, "stale": False}
    seen = []
    async def runtime(request):
        seen.append(request.url.path)
        assert request.url.path == "/internal/credentials" and request.method == "GET"
        assert request.headers["authorization"] == "Bearer " + common
        if state["offline"]: raise httpx.ConnectError("SYNTHETIC_PRIVATE_ERROR")
        kind = "chat" if request.url.port == 7860 else "profiler"
        provider = sources[kind].status()
        if state["stale"]: provider["revision"] = "stale-revision"
        return httpx.Response(200, json={"service": kind, "environment": state["environment"], "provider": provider,
                                         "backend": ServiceToken.from_environment(kind).status()})
    async def case():
        await app.state.backend.http.aclose()
        app.state.backend.http = httpx.AsyncClient(transport=httpx.MockTransport(runtime))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000",
                                     headers={"Origin": "http://127.0.0.1:9000"}) as client:
            assert (await client.get("/api/admin/provider-key/profiler")).status_code == 401
            response = await client.post("/api/admin/login", json={"password": password})
            client.headers["X-CSRF-Token"] = response.json()["csrf"]
            for kind in sources:
                endpoint = "/api/admin/provider-key/" + kind
                secret = "sk-or-v1-" + ("synthetic_" + kind) * 4
                saved = await client.put(endpoint, json={"key": secret, "revision": None, "confirmMigration": False})
                assert saved.status_code == 200 and saved.json()["application"] == {kind: "unverified"}
                observed = (await client.get(endpoint)).json()
                assert observed["application"] == {kind: "observed"} and observed["environment"] == "develop"
                assert secret not in saved.text + json.dumps(observed)
                state["stale"] = True
                assert (await client.get(endpoint)).json()["application"] == {kind: "pending"}
                state["stale"] = False; state["environment"] = "production"
                assert (await client.get(endpoint)).json()["application"] == {kind: "unverified"}
                state["environment"] = "develop"; state["offline"] = True
                assert (await client.get(endpoint)).json()["application"] == {kind: "unverified"}
                state["offline"] = False
            shared = (await client.get("/api/admin/backend-token")).json()
            assert shared["application"] == {"chat": "observed", "profiler": "observed"}
            assert common not in json.dumps(shared)
            assert (await client.put("/api/admin/provider-key/other", json={})).status_code == 404
        await app.state.backend.http.aclose()
    asyncio.run(case())
    assert seen and all(path == "/internal/credentials" for path in seen)


def test_injected_initial_key_can_be_replaced_from_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_MANAGED_KEY_DIR", str(tmp_path))
    monkeypatch.setenv("PROFILING_OPENROUTER_API_KEY", "sk-or-v1-" + "synthetic_injected_" * 4)
    path, password = fixture()
    app = create_app({"adminAuth": {"passwordHashFile": str(path)}})
    async def case():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000",
                                     headers={"Origin": "http://127.0.0.1:9000"}) as client:
            response = await client.post("/api/admin/login", json={"password": password})
            client.headers["X-CSRF-Token"] = response.json()["csrf"]
            response = await client.get("/api/admin/provider-key/profiler")
            assert response.json()["source"] == "deployment" and response.json()["writable"]
            assert response.json()["revision"] is None
            response = await client.put("/api/admin/provider-key/profiler", json={"key": "sk-or-v1-" + "synthetic_new_" * 4, "revision": None})
            assert response.status_code == 200 and "synthetic" not in response.text
            assert response.json()["source"] == "dashboard"
            assert ProviderKey.from_environment("profiler").snapshot().value == "sk-or-v1-" + "synthetic_new_" * 4
        await app.state.backend.http.aclose()
    asyncio.run(case())


def test_console_uses_deployment_model_and_budget_defaults_without_changing_explicit_config(monkeypatch):
    monkeypatch.setenv("AI_BUDGET_MODE", "provider-key-limits")
    monkeypatch.setenv("CHAT_MODEL", "synthetic/chat")
    monkeypatch.setenv("PROFILING_TAGGER_MODEL", "synthetic/profiler")
    async def case():
        supplied = {"expectedChatModel": "synthetic/explicit", "budgetMode": "guarded"}
        app = create_app(supplied)
        assert app.state.backend.config["expectedChatModel"] == "synthetic/explicit"
        assert app.state.backend.config["expectedProfilerModel"] == "synthetic/profiler"
        assert app.state.backend.config["budgetMode"] == "guarded"
        assert app.state.backend.budget_policy_matches({"budgetMode": "guarded"})
        assert not app.state.backend.budget_policy_matches({"budgetMode": "provider-key-limits"})
        assert supplied == {"expectedChatModel": "synthetic/explicit", "budgetMode": "guarded"}
        await app.state.backend.http.aclose()
        await app.state.provider_usage.close()
    asyncio.run(case())


def test_deployment_defaults_do_not_turn_ui_demo_into_unconfigured_service_client(monkeypatch):
    monkeypatch.setenv("AI_BUDGET_MODE", "provider-key-limits")
    monkeypatch.setenv("CHAT_MODEL", "synthetic/chat")
    path, _ = fixture()
    async def case():
        for config in (None, {"adminAuth": {"passwordHashFile": str(path)}}):
            app = create_app(config)
            assert app.state.backend.config == {}
            await app.state.backend.http.aclose()
            await app.state.provider_usage.close()
    asyncio.run(case())
