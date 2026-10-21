"""Existing ledger faults must fail closed without any provider transport."""

import json

import httpx
import pytest
from app.budget_ledger import BudgetLedger
from app.errors import APIError
from app.task_budget import BudgetHTTP


@pytest.mark.parametrize("payload", [None, [], 5, "unexpected"])
async def test_malformed_success_envelope_is_service_unavailable(payload):
    ledger = BudgetLedger(
        "http://127.0.0.1:9999",
        "synthetic",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=json.dumps(payload))
        ),
    )
    try:
        with pytest.raises(APIError) as error:
            await ledger.call("budget_summary", account="synthetic-budget")
        assert error.value.status == 503
    finally:
        await ledger.close()


@pytest.mark.parametrize(
    "mode", ["lost_ack", "malformed", "false_ack", "throttled", "unavailable"]
)
async def test_no_provider_send_without_reservation_ack(mode):
    paid = []

    def gateway(request):
        if mode == "lost_ack":
            raise httpx.ReadTimeout("synthetic acknowledgement lost", request=request)
        if mode == "malformed":
            return httpx.Response(200, json=[])
        if mode == "false_ack":
            return httpx.Response(200, json={"result": False})
        return httpx.Response(429 if mode == "throttled" else 503, json={})

    ledger = BudgetLedger(
        "http://127.0.0.1:9999", "synthetic", transport=httpx.MockTransport(gateway)
    )
    async with httpx.AsyncClient(
        base_url="https://synthetic-provider.invalid",
        transport=httpx.MockTransport(
            lambda r: paid.append(r) or httpx.Response(200, json={})
        ),
    ) as provider:
        try:
            with pytest.raises(APIError):
                await BudgetHTTP(provider, ledger).post(
                    "/chat/completions",
                    json={
                        "model": "openai/gpt-6-luna",
                        "max_tokens": 1200,
                        "messages": [],
                    },
                )
            assert paid == []
        finally:
            await ledger.close()


async def test_bridge_starts_unready_and_cannot_send_before_ledger_attaches(monkeypatch):
    from app import main
    from app.runtime import Runtime

    monkeypatch.setenv("STORE_MODE", "bridge")
    monkeypatch.setenv("CHAT_RUNTIME_MODE", "live")
    monkeypatch.setenv("AI_BUDGET_MODE", "guarded")
    monkeypatch.delenv("BUDGET_STORE_URL", raising=False)
    sent = []

    class Search:
        async def close(self):
            pass

    def runtime(store, search, owner):
        value = Runtime(store, search, owner)
        value.http = httpx.AsyncClient(base_url="https://synthetic.invalid",
            transport=httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200, json={})))
        return value

    monkeypatch.setattr(main, "SearchClient", lambda *args, **kwargs: Search())
    monkeypatch.setattr(main, "LowLatencyRuntime", runtime)
    async with main.lifespan(main.app):
        assert main.app.state.store.socket is None
        assert not main.app.state.runtime.healthy
        with pytest.raises(APIError) as error:
            await main.ready()
        assert error.value.status == 503
        with pytest.raises(APIError):
            await main.app.state.runtime.http.post("/chat/completions", json={
                "model": "openai/gpt-6-luna", "max_tokens": 1200, "messages": []})
        assert sent == []


@pytest.mark.parametrize("reported", [5, .75])
async def test_deferred_bridge_checks_limit_before_each_reservation(reported):
    calls, sent = [], []

    class Store:
        async def call(self, method, **args):
            assert args["account"] == "team-review-budget"
            calls.append(method)
            return {"limitUsd": reported} if method == "budget_summary" else True

    async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(
        lambda request: sent.append(request) or httpx.Response(200, json={"usage": {"cost": None}}))) as provider:
        budget = BudgetHTTP(provider, Store(), account="team-review-budget", expected_limit=.75, check_summary=True)
        body = {"model": "openai/gpt-6-luna", "max_tokens": 1200, "messages": []}
        if reported == .75:
            await budget.post("/chat/completions", json=body)
            assert calls == ["budget_summary", "budget_reserve", "budget_finish"] and len(sent) == 1
        else:
            with pytest.raises(APIError):
                await budget.post("/chat/completions", json=body)
            assert calls == ["budget_summary"] and sent == []
