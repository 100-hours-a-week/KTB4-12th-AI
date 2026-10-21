"""Operations collector is read-only, bounded and independent per data source."""

import asyncio
import json

import httpx
import psycopg
import pytest

from console.server import Backend, http_masked, public_health

BUDGET = {
    "limitUsd": 5,
    "committedUsd": 3.6,
    "remainingUsd": 1.4,
    "attempts": 144,
    "halted": None,
    "validationAttempts": 4,
    "validationReservedUsd": 0.05,
    "validationMaxAttempts": 100,
    "validationMaxReservedUsd": 1,
}


def test_health_allowlist_strips_unexpected_fields_at_every_level():
    raw = {
        "status": "ok",
        "prompt": "PRIVATE_PROMPT",
        "giftPreference": "PRIVATE_TASTE",
        "callbackUrl": "http://private.invalid?token=PRIVATE_QUERY",
        "catalog": {
            "active": True,
            "version": "synthetic",
            "products": 4231,
            "secret": "PRIVATE_CATALOG",
        },
        "store": {"connected": True, "providerResponse": "PRIVATE_RESPONSE"},
        "tagger": {
            "enabled": True,
            "model": "openai/gpt-6-luna",
            "prompt": "PRIVATE_PROMPT",
        },
    }
    result = public_health("profiling", raw)
    assert result["catalog"]["active"] is True and result["store"]["connected"] is True
    assert "PRIVATE_" not in json.dumps(result)
    assert "PRIVATE_" not in json.dumps(http_masked(raw))


def test_db_failure_does_not_erase_independently_collected_central_budget(monkeypatch):
    async def case():
        backend = Backend(
            {
                "liveModel": True,
                "budgetMode": "guarded",
                "database": "synthetic-never-connect",
                "targets": {
                    "chat": "http://127.0.0.1:1",
                    "search": "http://127.0.0.1:2",
                    "profiling": "http://127.0.0.1:3",
                },
            }
        )
        await backend.http.aclose()

        def remote(request):
            if request.url.port == 1:
                return httpx.Response(
                    200,
                    json={
                        "ready": True,
                        "mode": "live",
                        "model": "openai/gpt-6-luna",
                        "sharedBudget": BUDGET,
                        "providerResponse": "PRIVATE_RESPONSE",
                    },
                )
            if request.url.path == "/v1/metadata":
                return httpx.Response(
                    200,
                    json={"model": "jina-synthetic", "giftPreference": "PRIVATE_TASTE"},
                )
            if request.url.port == 2:
                return httpx.Response(
                    200,
                    json={
                        "status": "ready",
                        "products": 4231,
                        "snapshotId": "synthetic",
                    },
                )
            return httpx.Response(
                503,
                json={
                    "error": {"message": "PRIVATE_ERROR"},
                    "callbackUrl": "http://private.invalid?token=PRIVATE_QUERY",
                },
            )

        backend.http = httpx.AsyncClient(transport=httpx.MockTransport(remote))

        def failed_db():
            raise psycopg.OperationalError("synthetic unavailable")

        monkeypatch.setattr(backend, "database_snapshot", failed_db)
        try:
            result = await backend.snapshot()
            assert result["sharedBudget"] == BUDGET
            assert result["jobs"] is None and result["activeCounts"] is None
            assert result["collectionStatus"] == "partial"
            assert result["services"]["chat"]["status"] == "ready"
            assert result["services"]["profiling"]["status"] == "unready"
            assert len(result["services"]) == 4
            assert "PRIVATE_" not in json.dumps(result)
            assert result["budgetSeparation"]["actualUsd"] is None
            assert result["budgetSeparation"]["unconfirmedUsd"] is None
        finally:
            await backend.http.aclose()

    asyncio.run(case())


def test_live_model_and_catalog_mismatch_is_not_green():
    async def case():
        backend = Backend(
            {
                "liveModel": True,
                "budgetMode": "guarded",
                "expectedModel": "openai/gpt-6-luna",
                "catalogVersion": "expected-generation",
                "searchSnapshotId": "expected-snapshot",
                "targets": {
                    "chat": "http://127.0.0.1:1",
                    "search": "http://127.0.0.1:2",
                    "profiling": "http://127.0.0.1:3",
                },
            }
        )
        await backend.http.aclose()

        def remote(request):
            if request.url.port == 1:
                return httpx.Response(
                    200,
                    json={
                        "ready": True,
                        "mode": "live",
                        "model": "openai/gpt-6-luna",
                        "sharedBudget": BUDGET,
                    },
                )
            if request.url.port == 2:
                return httpx.Response(
                    200,
                    json={
                        "status": "ready",
                        "products": 4231,
                        "snapshotId": "different-snapshot",
                        "model": "synthetic",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "catalog": {"active": True, "version": "different-generation"},
                    "store": {"connected": True},
                    "tagger": {
                        "enabled": True,
                        "required": True,
                        "model": "different-model",
                    },
                    "embedding": {"enabled": True},
                },
            )

        backend.http = httpx.AsyncClient(transport=httpx.MockTransport(remote))
        try:
            result = await backend.snapshot()
            assert result["services"]["chat"]["status"] == "ready"
            assert result["services"]["search"]["status"] == "unready"
            assert result["services"]["profiling"]["status"] == "unready"
        finally:
            await backend.http.aclose()

    asyncio.run(case())


@pytest.mark.parametrize("mode", [None, "provider-key-limits", "guarded"])
def test_normal_dashboard_does_not_require_old_ledger_tables(monkeypatch, mode):
    statements = []

    class DB:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, statement):
            statements.append(statement)
            if "task_budget_attempts" in statement:
                assert mode == "guarded", "Normal dashboard must not query the old ledger"
            return self
        def fetchall(self): return []
        def fetchone(self):
            return {"n": 0, "actual": None, "reserved": None, "unconfirmed_n": 0}

    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: DB())
    async def case():
        config = {"database": "synthetic-no-connection"}
        if mode is not None:
            config["budgetMode"] = mode
        backend = Backend(config)
        try:
            snapshot = backend.database_snapshot()
            assert snapshot["activeCounts"] == {"chat": 0, "profiling": 0}
            assert snapshot["jobs"] == []
            assert (snapshot["budget"] is not None) is (mode == "guarded")
            assert any("task_budget_attempts" in s for s in statements) is (mode == "guarded")
        finally:
            await backend.http.aclose()
    asyncio.run(case())
