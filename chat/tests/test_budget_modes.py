"""Free startup/send-boundary checks using synthetic stores and HTTP transports."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from app import main
from app.errors import APIError
from app.model_trace import TRACE
from app.task_budget import ACCOUNT, BudgetHTTP
from starlette.datastructures import State

BODY = {"model": "openai/gpt-6-luna", "messages": [], "max_tokens": 32}


@pytest.fixture
async def deployment(monkeypatch):
    state = SimpleNamespace(calls=[], sent=[], stores=[], summary_error=None,
                            reserve_error=None, limit=5, redirect=False)
    monkeypatch.setattr(main.app, "state", State())
    monkeypatch.setenv("CHAT_RUNTIME_MODE", "live")
    monkeypatch.setenv("STORE_MODE", "http")
    monkeypatch.setenv("STORE_URL", "https://synthetic-store.invalid")
    monkeypatch.setenv("CHAT_MODEL", BODY["model"])
    for name in ("AI_BUDGET_MODE", "BUDGET_STORE_URL", "BE_SERVICE_TOKEN_DIR",
                 "CHAT_BE_SERVICE_TOKEN_FILE", "OPENROUTER_MANAGED_KEY_DIR",
                 "OPENROUTER_API_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)

    class Store:
        async def call(self, method, **kwargs):
            state.calls.append((method, kwargs))
            if method == "budget_summary":
                if state.summary_error:
                    raise state.summary_error
                return {"limitUsd": state.limit}
            if method == "budget_reserve" and state.reserve_error:
                raise state.reserve_error
            if method == "lease" and kwargs.get("inspect"):
                return {"active": False, "owner": "synthetic-owner"}
            return True

        async def close(self):
            pass

    def store(*args, **kwargs):
        value = Store()
        state.stores.append(value)
        return value

    class Search:
        async def ready(self):
            return {"status": "ready"}

        async def close(self):
            pass

    async def provider(transport, request):
        # Override the transport itself: even an accidental ledger URL cannot
        # reach a real socket or silently masquerade as a provider response.
        assert request.url.host == "openrouter.ai"
        state.sent.append(request)
        if state.redirect:
            return httpx.Response(307, headers={"Location": "https://elsewhere.invalid/"})
        if request.method == "GET":
            return httpx.Response(200, json={"data": {}})
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, text='data: {"usage":{"cost":0.001}}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={"usage": {"cost": 0.001}})

    monkeypatch.setattr(main, "RemoteStore", store)
    monkeypatch.setattr(main, "SearchClient", lambda *args, **kwargs: Search())
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", provider)
    yield state
    # Startup failures happen before lifespan's yield/finally. Close their
    # synthetic clients too, without changing production startup behavior here.
    if hasattr(main.app.state, "runtime"):
        await main.app.state.runtime.close()


async def wait_healthy():
    for _ in range(10):
        if main.app.state.runtime.healthy:
            return
        await asyncio.sleep(0)
    assert main.app.state.runtime.healthy


def budget_calls(state):
    return [(method, args) for method, args in state.calls if method.startswith("budget_")]


async def test_explicit_guarded_reserves_before_send(deployment, monkeypatch):
    monkeypatch.setenv("AI_BUDGET_MODE", "guarded")
    monkeypatch.delenv("BUDGET_ACCOUNT", raising=False)
    monkeypatch.delenv("BUDGET_EXPECTED_LIMIT_USD", raising=False)
    async with main.lifespan(main.app):
        await wait_healthy()
        runtime = main.app.state.runtime
        assert isinstance(runtime.http, BudgetHTTP)
        assert (await main.ready())["budgetMode"] == "guarded"
        trace = []
        token = TRACE.set(trace)
        try:
            await runtime.http.post("/chat/completions", json=BODY)
        finally:
            TRACE.reset(token)
        assert [method for method, _ in budget_calls(deployment)] == [
            "budget_summary", "budget_reserve", "budget_finish"]
        assert all(args["account"] == ACCOUNT for _, args in budget_calls(deployment))
        assert len(deployment.sent) == 1 and trace[0]["reservationId"]
        assert json.loads(deployment.sent[0].content)["provider"]["max_price"]


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("mode", [None, "", "  ", "provider-key-limits"])
async def test_provider_limits_has_no_ledger_and_preserves_auth_trace(
        deployment, monkeypatch, tmp_path, managed, mode):
    if mode is not None:
        monkeypatch.setenv("AI_BUDGET_MODE", mode)
    # Default and explicit provider mode do not contact a configured old gateway.
    monkeypatch.setenv("BUDGET_STORE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("BUDGET_ACCOUNT", "invalid account")
    monkeypatch.setenv("BUDGET_EXPECTED_LIMIT_USD", "invalid limit")
    deployment.summary_error = AssertionError("Provider mode must not read a ledger")
    expected_key = "test-only-not-a-real-key"
    if managed:
        from integration.provider_key import ProviderKey
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_MANAGED_KEY_DIR", str(tmp_path))
        source = ProviderKey.from_environment()
        expected_key = "sk-or-v1-" + "synthetic-key-a" * 3
        saved = source.save(expected_key, None)
    async with main.lifespan(main.app):
        await wait_healthy()
        runtime = main.app.state.runtime
        assert isinstance(runtime.http, httpx.AsyncClient)
        assert runtime.catalog.http is runtime.http
        async with httpx.AsyncClient(transport=httpx.ASGITransport(main.app), base_url="https://test") as client:
            assert (await client.get("/readyz")).status_code == 401
            response = await client.get("/readyz", headers={"Authorization": "Bearer test-service-token"})
        assert response.status_code == 200
        status = response.json()
        assert status["budgetMode"] == "provider-key-limits"
        assert "sharedBudget" not in status and "budgetAccount" not in status
        trace = []
        token = TRACE.set(trace)
        try:
            await runtime.http.post("/chat/completions", json=BODY)
            assert deployment.sent[-1].headers["Authorization"] == "Bearer " + expected_key
            if managed:
                expected_key = "sk-or-v1-" + "synthetic-key-b" * 3
                source.save(expected_key, saved["revision"])
            async with runtime.http.stream("POST", "/chat/completions", json={**BODY, "stream": True}) as response:
                assert "[DONE]" in (await response.aread()).decode()
            await runtime.http.get("/model/openai/gpt-6-luna")
        finally:
            TRACE.reset(token)
        assert len(deployment.sent) == 3
        assert deployment.sent[-2].headers["Authorization"] == "Bearer " + expected_key
        assert len(trace) == 2 and [item["stream"] for item in trace] == [False, True]
        assert all(item["reservationId"] is None and "messages" not in item for item in trace)
        assert all("max_price" not in json.loads(request.content).get("provider", {})
                   for request in deployment.sent if request.method == "POST")
    assert budget_calls(deployment) == []


@pytest.mark.parametrize("mode", ["disabled", "GUARDED", "auto"])
async def test_invalid_mode_rejected_before_clients(deployment, monkeypatch, mode):
    monkeypatch.setenv("AI_BUDGET_MODE", mode)
    with pytest.raises(ValueError, match="AI_BUDGET_MODE"):
        async with main.lifespan(main.app):
            pytest.fail("Invalid mode started")
    assert deployment.stores == [] and deployment.sent == []


@pytest.mark.parametrize("fault", ["unavailable", "wrong-limit"])
async def test_guarded_startup_does_not_fall_back(deployment, monkeypatch, fault):
    monkeypatch.setenv("AI_BUDGET_MODE", "guarded")
    if fault == "unavailable":
        deployment.summary_error = APIError(503, "synthetic ledger unavailable")
    else:
        deployment.limit = 4
    with pytest.raises((APIError, ValueError)):
        async with main.lifespan(main.app):
            pytest.fail("Unverified guarded ledger started")
    assert [method for method, _ in budget_calls(deployment)] == ["budget_summary"]
    assert deployment.sent == []


async def test_guarded_reservation_failure_prevents_send(deployment, monkeypatch):
    monkeypatch.setenv("AI_BUDGET_MODE", "guarded")
    deployment.reserve_error = APIError(503, "synthetic reservation unavailable")
    async with main.lifespan(main.app):
        with pytest.raises(APIError):
            await main.app.state.runtime.http.post("/chat/completions", json=BODY)
        assert deployment.sent == []
        assert [method for method, _ in budget_calls(deployment)] == ["budget_summary", "budget_reserve"]


@pytest.mark.parametrize("url", [
    "https://elsewhere.invalid/api/v1/chat/completions",
    "http://openrouter.ai/api/v1/chat/completions",
    "https://openrouter.ai:444/api/v1/chat/completions",
    "https://user:password@openrouter.ai/api/v1/chat/completions",
    "https://openrouter.ai/api/v1/chat/completions?key=synthetic",
    "https://openrouter.ai/api/v1/other",
])
async def test_provider_mode_rejects_unapproved_destination(deployment, monkeypatch, url):
    monkeypatch.setenv("AI_BUDGET_MODE", "provider-key-limits")
    async with main.lifespan(main.app):
        with pytest.raises(APIError):
            await main.app.state.runtime.http.post(url, json=BODY)
        assert deployment.sent == [] and budget_calls(deployment) == []


async def test_provider_mode_does_not_follow_redirects(deployment, monkeypatch):
    monkeypatch.setenv("AI_BUDGET_MODE", "provider-key-limits")
    deployment.redirect = True
    async with main.lifespan(main.app):
        response = await main.app.state.runtime.http.post("/chat/completions", json=BODY)
        assert response.status_code == 307 and len(deployment.sent) == 1
        assert budget_calls(deployment) == []


async def test_provider_mode_missing_managed_key_is_unready(deployment, monkeypatch, tmp_path):
    from integration.provider_key import KeyUnavailable
    monkeypatch.setenv("AI_BUDGET_MODE", "provider-key-limits")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_MANAGED_KEY_DIR", str(tmp_path))
    async with main.lifespan(main.app):
        await wait_healthy()
        with pytest.raises(APIError) as error:
            await main.ready()
        assert error.value.status == 503
        with pytest.raises(KeyUnavailable):
            await main.app.state.runtime.http.post("/chat/completions", json=BODY)
        assert deployment.sent == [] and budget_calls(deployment) == []
