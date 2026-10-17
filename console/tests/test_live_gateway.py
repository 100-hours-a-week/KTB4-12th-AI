import asyncio
import importlib.util
from pathlib import Path

import httpx


def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_TOKEN", "SYNTHETIC_GATEWAY_TOKEN")
    monkeypatch.setenv("LIVE_VALIDATION_BUDGET_DB", str(tmp_path / "attempts.sqlite3"))
    spec = importlib.util.spec_from_file_location(
        "synthetic_gateway", Path(__file__).parents[2] / "integration/budget_gateway.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_limit_is_additional_to_central_ledger(tmp_path, monkeypatch):
    module = gateway(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        module, "remote", lambda method, args: calls.append((method, args)) or True
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(module.app), base_url="http://127.0.0.1:1"
        ) as client:
            for i in range(7):
                response = await client.post(
                    "/rpc",
                    headers={"Authorization": "Bearer SYNTHETIC_GATEWAY_TOKEN"},
                    json={
                        "method": "budget_reserve",
                        "args": {
                            "account": module.ACCOUNT,
                            "ident": str(i),
                            "amount": 0.01,
                            "model": "openai/gpt-6-luna",
                        },
                    },
                )
                assert response.status_code == (200 if i < 6 else 429)
            assert len(calls) == 6
            assert all(method == "budget_reserve" for method, args in calls)

    asyncio.run(run())


def test_lost_central_ack_never_becomes_a_paid_send_permission(tmp_path, monkeypatch):
    module = gateway(tmp_path, monkeypatch)
    calls = []

    def lose_ack(method, args):
        calls.append(method)
        raise OSError("synthetic lost acknowledgement")

    monkeypatch.setattr(module, "remote", lose_ack)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(module.app), base_url="http://127.0.0.1:1"
        ) as client:
            request = {
                "method": "budget_reserve",
                "args": {
                    "account": module.ACCOUNT,
                    "ident": "same",
                    "amount": 0.01,
                    "model": "openai/gpt-6-luna",
                },
            }
            for _ in range(2):
                assert (
                    await client.post(
                        "/rpc",
                        headers={"Authorization": "Bearer SYNTHETIC_GATEWAY_TOKEN"},
                        json=request,
                    )
                ).status_code == 503
            assert calls == ["budget_reserve"]
            assert (
                await client.post(
                    "/rpc",
                    headers={"Authorization": "Bearer SYNTHETIC_GATEWAY_TOKEN"},
                    json={
                        "method": "budget_import",
                        "args": {"account": module.ACCOUNT},
                    },
                )
            ).status_code == 503

    asyncio.run(run())


def test_approved_guard_increase_preserves_existing_attempts(tmp_path, monkeypatch):
    import sqlite3

    first = gateway(tmp_path, monkeypatch)
    with sqlite3.connect(first.PATH) as db:
        for i in range(4):
            db.execute(
                "INSERT INTO attempts VALUES(?,?,?,1)",
                (str(i), 0.0125, "openai/gpt-6-luna"),
            )
    monkeypatch.setenv("LIVE_VALIDATION_MAX_ATTEMPTS", "100")
    monkeypatch.setenv("LIVE_VALIDATION_MAX_RESERVED_USD", "1")
    module = gateway(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "remote",
        lambda method, args: {"limitUsd": 5} if method == "budget_summary" else True,
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(module.app), base_url="http://127.0.0.1:1"
        ) as client:
            headers = {"Authorization": "Bearer SYNTHETIC_GATEWAY_TOKEN"}
            result = (
                await client.post(
                    "/rpc",
                    headers=headers,
                    json={
                        "method": "budget_summary",
                        "args": {"account": module.ACCOUNT},
                    },
                )
            ).json()["result"]
            assert result["validationAttempts"] == 4
            assert result["validationReservedUsd"] == 0.05
            assert result["validationMaxAttempts"] == 100
            assert result["validationMaxReservedUsd"] == 1
            response = await client.post(
                "/rpc",
                headers=headers,
                json={
                    "method": "budget_reserve",
                    "args": {
                        "account": module.ACCOUNT,
                        "ident": "too-large",
                        "amount": 0.11,
                        "model": "openai/gpt-6-luna",
                    },
                },
            )
            assert response.status_code == 503
            with sqlite3.connect(module.PATH) as db:
                assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 4

    asyncio.run(run())
