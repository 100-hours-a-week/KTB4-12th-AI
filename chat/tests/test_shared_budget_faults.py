"""Existing ledger faults must fail closed without any provider transport."""

import json

import httpx
import pytest
from app.budget_ledger import BudgetLedger
from app.errors import APIError
from app.task_budget import ACCOUNT, BudgetHTTP


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
            await ledger.call("budget_summary", account=ACCOUNT)
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
