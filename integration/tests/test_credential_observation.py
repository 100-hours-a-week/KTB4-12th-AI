"""Runtime evidence uses synthetic credentials and mocked HTTP; no external IO."""
from datetime import datetime
import hashlib
import json

import httpx
import pytest

from integration.provider_key import ProviderKey
from integration.profiler_boundary import ProviderLimitedExtractor
from profiling.ports import TagExtractError


def key(label):
    return "sk-or-v1-" + ("synthetic_" + label + "_") * 4


def test_success_belongs_to_current_revision_and_process_without_file_writes(tmp_path):
    source = ProviderKey(managed_directory=tmp_path)
    assert source.runtime_status()["usage"]["state"] == "not-observed"
    saved = source.save(key("first"), None)
    original = source.identity(source.snapshot())
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert "usage" not in source.status()
    assert source.runtime_status()["usage"]["lastSuccessfulCallAt"] is None
    source.record_success(original)
    usage = source.runtime_status()["usage"]
    assert usage["state"] == "succeeded" and usage["scope"] == "responding-process"
    assert datetime.fromisoformat(usage["lastSuccessfulCallAt"]).tzinfo is not None
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert ProviderKey(managed_directory=tmp_path).runtime_status()["usage"]["state"] == "not-observed"
    # Even saving the same value again establishes a new managed revision.
    source.save(key("first"), saved["revision"])
    assert source.runtime_status()["usage"]["lastSuccessfulCallAt"] is None
    source.record_success(source.identity(source.snapshot()))
    current = source.runtime_status()["usage"]
    source.record_success(original)  # Late old completion cannot replace new evidence.
    assert source.runtime_status()["usage"] == current
    wire = json.dumps(source.runtime_status())
    assert key("first") not in wire and hashlib.sha256(key("first").encode()).hexdigest() not in wire
    (tmp_path / source.record_file).unlink()
    assert source.runtime_status()["usage"]["state"] == "not-observed"


def test_deployment_file_change_without_revision_does_not_inherit_success(tmp_path):
    path = tmp_path / "synthetic-injected-key"
    path.write_text(key("first"))
    source = ProviderKey(external_file=path)
    source.record_success(source.identity(source.snapshot()))
    assert source.runtime_status()["source"] == "deployment"
    assert source.runtime_status()["revision"] is None
    assert source.runtime_status()["usage"]["state"] == "succeeded"
    path.write_text(key("next"))
    assert source.runtime_status()["usage"]["state"] == "not-observed"


def test_other_service_or_environment_success_is_not_accepted(tmp_path):
    source = ProviderKey(kind="chat", managed_directory=tmp_path)
    source.save(key("chat"), None)
    profiler = ProviderKey(kind="profiler", managed_directory=tmp_path)
    profiler.save(key("chat"), None)
    source.record_success(profiler.identity(profiler.snapshot()))
    assert source.runtime_status()["usage"]["state"] == "not-observed"
    deployed = ProviderKey(value=key("chat"), env="develop")
    source.record_success(deployed.identity(deployed.snapshot()))
    assert source.runtime_status()["usage"]["state"] == "not-observed"


@pytest.mark.parametrize("status,body,expected", [
    (200, {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}, "succeeded"),
    (200, {"choices": [{"message": {"content": "{}"}}]}, "not-observed"),
    (200, {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]}, "not-observed"),
    (200, {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "error": "synthetic"}, "not-observed"),
    (200, {"choices": []}, "not-observed"),
    (401, {}, "not-observed"), (402, {}, "not-observed"),
    (429, {}, "not-observed"), (500, {}, "not-observed"),
])
def test_profiler_records_only_completed_model_calls(tmp_path, status, body, expected):
    source = ProviderKey(kind="profiler", managed_directory=tmp_path)
    source.save(key("profiler"), None)
    extractor = ProviderLimitedExtractor("https://openrouter.ai/api/v1", "", "synthetic/model",
        credential_source=source, transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)))
    try:
        try:
            extractor.extract("synthetic", "synthetic", {})
        except TagExtractError:
            pass
        assert source.runtime_status()["usage"]["state"] == expected
    finally:
        extractor.close()


def test_profiler_inflight_old_key_cannot_verify_replacement(tmp_path):
    source = ProviderKey(kind="profiler", managed_directory=tmp_path)
    saved = source.save(key("old"), None)
    calls = []
    def respond(request):
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            source.save(key("new"), saved["revision"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
    extractor = ProviderLimitedExtractor("https://openrouter.ai/api/v1", "", "synthetic/model",
        credential_source=source, transport=httpx.MockTransport(respond))
    try:
        extractor.extract("synthetic", "synthetic", {})
        assert source.runtime_status()["usage"]["state"] == "not-observed"
        extractor.extract("synthetic", "synthetic", {})
        assert source.runtime_status()["usage"]["state"] == "succeeded"
        assert calls == ["Bearer " + key("old"), "Bearer " + key("new")]
    finally:
        extractor.close()


def test_profiler_endpoint_separates_provider_evidence_from_backend_uncollected(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from integration.profiler_runtime import credential_status
    from integration.service_token import ServiceToken

    monkeypatch.setenv("BE_SERVICE_TOKEN_DIR", str(tmp_path))
    monkeypatch.delenv("AI_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("PROFILING_SERVICE_TOKEN", raising=False)
    token = "synthetic_shared_backend_token_" * 2
    ServiceToken("profiler", managed_directory=tmp_path).save(token, None)
    source = ProviderKey(kind="profiler", managed_directory=tmp_path)
    source.save(key("profiler"), None)
    app = FastAPI()
    app.state.provider_key = source
    app.get("/internal/credentials")(credential_status)
    with TestClient(app) as client:
        assert client.get("/internal/credentials").status_code == 401
        response = client.get("/internal/credentials", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        body = response.json()
        assert body["provider"]["usage"] == {"state": "not-observed", "lastSuccessfulCallAt": None,
                                             "scope": "responding-process"}
        assert body["backend"]["usage"]["state"] == "not-collected"
        assert token not in response.text and key("profiler") not in response.text
