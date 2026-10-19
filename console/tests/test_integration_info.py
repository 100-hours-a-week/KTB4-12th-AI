"""BE connection information uses deployment values and existing observations only."""
import asyncio
import json

import httpx
import pytest

from console.server import be_api_address, integration_readiness
from console.tests.auth_support import configured_app, login


@pytest.mark.parametrize("value", [None, ""])
def test_missing_address_remains_unconfigured(value):
    assert be_api_address(value, "/route") == {"state": "missing", "url": None}


@pytest.mark.parametrize("value", [
    "https://user:SYNTHETIC_SECRET@example.invalid", "https://SYNTHETIC_SECRET@example.invalid",
    "https://example.invalid?token=SYNTHETIC_SECRET", "https://example.invalid?",
    "https://example.invalid#SYNTHETIC_SECRET", "https://example.invalid/%53YNTHETIC_SECRET",
    "javascript:alert(1)", "//example.invalid", "ftp://example.invalid", "https://example.invalid:99999",
    "https://example.invalid:0", "https://example.invalid/../private", "https://example.invalid/./private",
    "https://example.invalid\\SYNTHETIC_SECRET", "https://example.invalid/\nsecret",
    " https://example.invalid", "https://bad..invalid", "https://bad_host", {}, [],
])
def test_invalid_or_secret_bearing_addresses_are_never_echoed(value):
    assert be_api_address(value, "/route") == {"state": "invalid", "url": None}


@pytest.mark.parametrize("value", ["https://ai.example.invalid", "https://ai.example.invalid/chat/", "http://127.0.0.1:8000", "http://[::1]:8000", "https://chat.internal:8443/prefix"])
def test_explicit_bases_allow_deployment_prefixes_without_dns_or_http(value):
    assert be_api_address(value, "/route") == {"state": "configured", "url": value.rstrip("/")+"/route"}


@pytest.mark.parametrize("env", ["local", "develop", "production"])
def test_authenticated_snapshot_uses_only_explicit_deployment_urls(monkeypatch, env):
    async def case():
        monkeypatch.setenv("AI_ENVIRONMENT", env)
        monkeypatch.setenv("CONSOLE_BE_CHAT_BASE_URL", "https://ai.example.invalid/chat")
        monkeypatch.setenv("CONSOLE_BE_PROFILER_BASE_URL", "https://ai.example.invalid/profiler")
        monkeypatch.setenv("APP_REVISION", "a"*40)
        app = configured_app()
        async def no_new_probe(*a, **kw): pytest.fail("Connection panel must not probe any address")
        app.state.backend.request = no_new_probe
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as client:
            assert (await client.get("/api/snapshot")).status_code == 401
            await login(client)
            response = await client.get("/api/snapshot", headers={"X-Forwarded-Host": "untrusted.invalid"})
            info = response.json()["integration"]
            assert info["environment"] == env and info["revision"] == "a"*40
            assert info["api"]["chat"]["url"] == "https://ai.example.invalid/chat/v1/chat/conversations/{conversationId}"
            from profiling.intake import EXTRACT_AND_POOL_PATH
            assert info["api"]["profiler"]["url"] == "https://ai.example.invalid/profiler"+EXTRACT_AND_POOL_PATH
            assert info["authentication"] == "Authorization: Bearer <service_token>"
            assert info["readiness"]["beConnection"] == "unverified"
            assert info["readiness"]["database"] == "synthetic"
            assert "untrusted.invalid" not in response.text
        await app.state.backend.http.aclose(); await app.state.provider_usage.close()
    asyncio.run(case())


def test_no_fallback_from_internal_targets_browser_or_invalid_config(monkeypatch):
    async def case():
        monkeypatch.delenv("CONSOLE_BE_CHAT_BASE_URL", raising=False)
        monkeypatch.setenv("CONSOLE_BE_PROFILER_BASE_URL", "https://SYNTHETIC_PASSWORD@example.invalid?token=SYNTHETIC_QUERY")
        monkeypatch.setenv("APP_REVISION", "SYNTHETIC_SECRET_REVISION")
        app = configured_app({"targets": {"chat": "http://127.0.0.1:7860"}, "tokens": {"chat": "SYNTHETIC_PRIVATE_TOKEN"}})
        async def snapshot():
            return {"collectedAt": "2026-10-19T00:00:00Z", "services": {"chat": {"status": "ready"}}, "dbError": "unavailable"}
        app.state.backend.snapshot = snapshot
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as client:
            await login(client)
            result = (await client.get("/api/snapshot")).json()["integration"]
            assert result["api"]["chat"] == {"method": "PUT", "state": "missing", "url": None}
            assert result["api"]["profiler"]["url"] is None
            assert result["api"]["profiler"]["state"] == "invalid"
            assert result["revisionSource"] == "console-build"
            assert result["readiness"] == {"database": "unready", "products": "unknown", "productCount": None, "beConnection": "unverified"}
            assert "SYNTHETIC_" not in json.dumps(result) and "127.0.0.1" not in json.dumps(result)
        await app.state.backend.http.aclose(); await app.state.provider_usage.close()
    asyncio.run(case())


def test_readiness_does_not_promote_missing_or_empty_data_to_verified_be():
    assert integration_readiness({})["database"] == "unknown"
    ready = {"dbSource": "read-only PostgreSQL", "services": {"search": {"status": "ready", "data": {"products": 12}}}}
    result = integration_readiness(ready)
    assert result == {"database": "ready", "products": "ready", "productCount": 12, "beConnection": "unverified"}
    ready["services"]["search"]["data"]["products"] = 0
    assert integration_readiness(ready)["products"] == "empty"
    ready["services"]["search"]["status"] = "unknown"
    assert integration_readiness(ready)["products"] == "unknown"
