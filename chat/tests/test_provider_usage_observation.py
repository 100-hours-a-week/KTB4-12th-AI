"""Actual Chat response parsing with synthetic HTTP responses only."""
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app.low_latency import ProviderAdapter
from app.runtime import Runtime
from integration.provider_key import ProviderKey


async def runtime_with(tmp_path, monkeypatch, handler):
    monkeypatch.setenv("OPENROUTER_MANAGED_KEY_DIR", str(tmp_path))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    source = ProviderKey(managed_directory=tmp_path)
    source.save("sk-or-v1-" + "synthetic_current_" * 3, None)
    runtime = Runtime(object(), object(), "synthetic-observer")
    auth = runtime.http._auth
    await runtime.http.aclose()
    runtime.http = httpx.AsyncClient(base_url="https://openrouter.ai/api/v1", auth=auth,
                                   transport=httpx.MockTransport(handler))
    runtime.model = "synthetic/model"
    runtime.catalog.http = runtime.http
    return runtime


@pytest.mark.asyncio
async def test_metadata_does_not_verify_but_model_completion_does(tmp_path, monkeypatch):
    runtime = await runtime_with(tmp_path, monkeypatch, lambda request: httpx.Response(200,
        json={"choices": [{"message": {"content": "synthetic answer"}, "finish_reason": "stop"}]}))
    try:
        await runtime.http.get("/models/synthetic/endpoints")
        assert runtime.provider_key.runtime_status()["usage"]["state"] == "not-observed"
        assert await runtime.complete([], []) == "synthetic answer"
        assert runtime.provider_key.runtime_status()["usage"]["state"] == "succeeded"
    finally:
        await runtime.close()


class BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"synthetic partial"},"finish_reason":"stop"}]}\n\n'
        raise httpx.ReadError("synthetic disconnected provider")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["baseline", "tool-loop"])
@pytest.mark.parametrize("outcome", ["complete", "incomplete", "interrupted", "http-error"])
async def test_stream_headers_or_partial_output_cannot_verify_key(tmp_path, monkeypatch, path, outcome):
    def respond(request):
        if outcome == "interrupted": return httpx.Response(200, stream=BrokenStream())
        if outcome == "http-error": return httpx.Response(402, json={"error": "synthetic"})
        packet = {"choices": [{"delta": {"content": "synthetic answer"},
                   "finish_reason": "stop" if outcome == "complete" else None}]}
        return httpx.Response(200, text="data: " + json.dumps(packet) + "\n\ndata: [DONE]\n\n")
    runtime = await runtime_with(tmp_path, monkeypatch, respond)
    async def emit(*args): pass
    runtime.emit = emit
    receipt = {"startedMonotonic": time.monotonic(), "usage": []}
    try:
        try:
            if path == "baseline":
                await runtime.stream_answer("synthetic-cid", "synthetic-tid", receipt,
                                            {"model": "synthetic/model", "stream": True})
            else:
                async for _ in ProviderAdapter(runtime, receipt).stream([], [], "synthetic/model"):
                    pass
        except (ValueError, httpx.HTTPError, RuntimeError):
            if outcome == "complete": raise
        assert runtime.provider_key.runtime_status()["usage"]["state"] == (
            "succeeded" if outcome == "complete" else "not-observed")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_chat_credential_endpoint_exposes_only_current_process_evidence(tmp_path, monkeypatch):
    from app import main
    from starlette.datastructures import State

    runtime = await runtime_with(tmp_path, monkeypatch, lambda request: httpx.Response(200,
        json={"choices": [{"message": {"content": "synthetic answer"}, "finish_reason": "stop"}]}))
    monkeypatch.setattr(main.app, "state", State({"runtime": SimpleNamespace(provider_key=runtime.provider_key)}))
    try:
        await runtime.complete([], [])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(main.app), base_url="http://synthetic") as client:
            response = await client.get("/internal/credentials", headers={"Authorization": "Bearer test-service-token"})
            assert response.status_code == 200
            body = response.json()
            assert body["provider"]["usage"]["state"] == "succeeded"
            assert body["provider"]["usage"]["scope"] == "responding-process"
            assert body["backend"]["usage"]["state"] == "not-collected"
            assert runtime.provider_key.snapshot().value not in response.text
    finally:
        await runtime.close()
