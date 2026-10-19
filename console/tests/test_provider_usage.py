"""Provider key allowance boundaries, rotation, caching, and admin-only access."""
import asyncio
import json
import os

import httpx
import pytest

from console.provider_usage import KEY_URL, MAX_BODY, ProviderUsage, amounts
from console.tests.auth_support import configured_app, login
from integration.provider_key import KeySnapshot, KeyUnavailable


def payload(**changes):
    return {"data": {"usage": 1.25, "limit": 10, "limit_remaining": 8.75, "limit_reset": None, **changes}}


class Source:
    def __init__(self, key):
        self.key = key

    def snapshot(self):
        if self.key is None:
            raise KeyUnavailable("PROVIDER_KEY_MISSING")
        if isinstance(self.key, Exception):
            raise self.key
        return KeySnapshot(self.key, scope=("local",))


def harness(handler):
    now = [100.0]
    sources = {kind: Source("synthetic_private_" + kind) for kind in ("chat", "profiler")}
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    usage = ProviderUsage(http=client, source=sources.__getitem__, clock=lambda: now[0], wall=lambda: 1700000000 + now[0])
    return usage, sources, now


def test_two_keys_single_flight_fixed_origin_and_private_response(caplog):
    requests = []
    async def handler(request):
        requests.append(request)
        assert request.method == "GET" and str(request.url) == KEY_URL
        assert request.content == b""
        return httpx.Response(200, json=payload(label="synthetic_private_label", key="must_not_leave_server", usage=2 if request.headers["authorization"].endswith("profiler") else 1))
    async def case():
        usage, sources, _ = harness(handler)
        results = await asyncio.gather(*(usage.status() for _ in range(12)))
        assert len(requests) == 2
        assert results[0]["chat"]["usageUsd"] == 1 and results[0]["profiler"]["usageUsd"] == 2
        assert results[0]["chat"]["state"] == "verified"
        assert results[0]["chat"]["checkedAt"]
        assert "synthetic_private" not in json.dumps(results) + caplog.text
        assert "must_not_leave_server" not in json.dumps(results) + caplog.text
        assert {r.headers["authorization"] for r in requests} == {"Bearer " + s.key for s in sources.values()}
        await usage.close()
    asyncio.run(case())


@pytest.mark.parametrize("fields,state,limit", [
    ({"usage": 0, "limit": 0, "limit_remaining": 0}, "verified", "limited"),
    ({"limit": None, "limit_remaining": None}, "unlimited", "unlimited"),
    ({"usage": None}, "partial", "limited"),
    ({"limit_remaining": None}, "partial", "limited"),
    ({"limit_remaining": -0.25}, "verified", "limited"),
])
def test_zero_unknown_unlimited_and_exhausted_remain_distinct(fields, state, limit):
    result = amounts(payload(**fields))
    assert result["state"] == state and result["limitState"] == limit
    assert result["remainingUsd"] == fields.get("limit_remaining", 8.75)


def test_missing_fields_are_unknown_and_reset_usage_is_never_subtracted():
    assert amounts({"data": {}})["state"] == "partial"
    assert amounts({"data": {}})["limitState"] == "unknown"
    result = amounts(payload(usage=500, limit=10, limit_remaining=8, limit_reset="monthly"))
    assert result["usageUsd"] == 500 and result["remainingUsd"] == 8 and result["limitReset"] == "monthly"


@pytest.mark.parametrize("value", [True, "1", -1, float("nan"), float("inf"), [], {}])
def test_invalid_usage_is_not_presented_as_verified(value):
    with pytest.raises(ValueError):
        amounts(payload(usage=value))


@pytest.mark.parametrize("status,code", [(401,"key_rejected"),(403,"key_rejected"),(429,"rate_limited"),(500,"provider_unavailable"),(302,"provider_unavailable")])
def test_failures_retain_same_key_values_with_original_timestamp_and_backoff(status, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload()) if len(calls) == 1 else httpx.Response(status, headers={"Location":"https://invalid.example/secret"}, text="synthetic_private_error")
    async def case():
        usage, _, now = harness(handler)
        first = await usage.get("chat")
        now[0] += 61
        failed = await usage.get("chat")
        assert failed["state"] == "failed" and failed["errorCode"] == code
        assert failed["checkedAt"] == first["checkedAt"] and failed["usageUsd"] == first["usageUsd"]
        assert failed["ageSeconds"] == 61 and failed["attemptedAt"] != first["attemptedAt"]
        assert "synthetic_private_error" not in json.dumps(failed)
        await usage.get("chat")
        assert len(calls) == 2
        await usage.close()
    asyncio.run(case())


def test_external_key_rotation_and_removal_never_reuse_old_amounts():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload()) if len(calls) == 1 else httpx.Response(401)
    async def case():
        usage, sources, _ = harness(handler)
        assert (await usage.get("chat"))["usageUsd"] == 1.25
        sources["chat"].key = "synthetic_private_new"
        changed = await usage.get("chat")
        assert changed["state"] == "failed" and changed["usageUsd"] is None and changed["checkedAt"] is None
        sources["chat"].key = None
        missing = await usage.get("chat")
        assert missing["state"] == "unconfigured" and missing["limitState"] == "unknown"
        sources["profiler"].key = KeyUnavailable("PROVIDER_KEY_SOURCE_CONFLICT")
        assert (await usage.get("profiler"))["state"] == "unavailable"
        assert len(calls) == 2
        await usage.close()
    asyncio.run(case())


def test_rotation_during_inflight_query_discards_old_response():
    async def case():
        started, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            started.set(); await release.wait()
            return httpx.Response(200, json=payload())
        usage, sources, _ = harness(handler)
        task = asyncio.create_task(usage.get("chat"))
        await started.wait()
        sources["chat"].key = "synthetic_private_replaced"
        release.set()
        result = await task
        assert result["errorCode"] == "key_changed" and result["usageUsd"] is None
        await usage.close()
    asyncio.run(case())


@pytest.mark.parametrize("body", [b"not-json", b"{}", b"x" * (MAX_BODY + 1)])
def test_bad_provider_body_is_bounded_and_never_exposed(body):
    async def case():
        usage, _, _ = harness(lambda request: httpx.Response(200, content=body))
        result = await usage.get("chat")
        assert result["state"] == "failed" and result["errorCode"] == "invalid_response"
        assert result["usageUsd"] is None and result["checkedAt"] is None
        await usage.close()
    asyncio.run(case())


def test_network_failure_is_sanitized_and_does_not_change_chat_readiness(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("AI_", "OPENROUTER_", "PROFILING_", "CONSOLE_ADMIN_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic_private_chat")
    monkeypatch.setenv("PROFILING_OPENROUTER_API_KEY", "synthetic_private_profiler")
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("synthetic_private_error", request=request)
    async def case():
        app = configured_app()
        await app.state.provider_usage.http.aclose()
        app.state.provider_usage.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as client:
            assert (await client.get("/api/admin/provider-usage")).status_code == 401 and not calls
            await login(client)
            before = (await client.get("/api/config")).json()
            assert not calls
            response = await client.get("/api/admin/provider-usage")
            assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
            assert all(value["state"] == "failed" for value in response.json().values())
            assert "synthetic_private" not in response.text
            assert (await client.get("/api/config")).json() == before
        await app.state.provider_usage.close()
        await app.state.backend.http.aclose()
    asyncio.run(case())


def test_empty_be_token_does_not_hide_independent_service_observations(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("AI_", "OPENROUTER_", "PROFILING_", "BE_", "CHAT_BE_", "PROFILER_BE_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("BE_SERVICE_TOKEN_DIR", str(tmp_path))
    async def case():
        app = configured_app({"targets": {kind: "http://127.0.0.1:12345" for kind in ("chat", "profiling", "search")},
                              "liveModel": True, "budgetMode": "provider-key-limits"})
        calls = []
        def upstream(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"status": "ready", "model": "synthetic-local-encoder"})
        await app.state.backend.http.aclose()
        app.state.backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as client:
            await login(client)
            response = await client.get("/api/snapshot")
            assert response.status_code == 200
            states = response.json()["services"]
            assert states["search"]["status"] == states["console"]["status"] == "ready"
            for service in ("chat", "profiling"):
                assert states[service]["status"] == "unready" and states[service]["httpStatus"] == 503
                assert states[service]["reasons"] == ["공통 BE 토큰을 설정해 주세요."]
            assert calls == ["/readyz", "/v1/metadata"]
        await app.state.provider_usage.close()
        await app.state.backend.http.aclose()
    asyncio.run(case())
