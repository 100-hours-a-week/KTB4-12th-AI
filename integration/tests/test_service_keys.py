"""No sockets or real credentials: two model keys, one BE token, and durable CAS."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from threading import Lock, Event
from types import SimpleNamespace
import uuid

import httpx
import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from integration.provider_key import ACTIVE_KEY, ProviderAuth, KeyConflict, KeyUnavailable, ProviderKey
from integration.service_token import ServiceToken


def key(label):
    return "sk-or-v1-" + ("synthetic_" + label + "_") * 4


@pytest.fixture(autouse=True)
def clear_credentials(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("AI_", "OPENROUTER_", "PROFILING_", "BE_", "CHAT_BE_", "PROFILER_BE_")):
            monkeypatch.delenv(name)


def test_service_sources_are_independent_and_legacy_file_is_preserved(tmp_path):
    old = tmp_path / "openrouter-key.json"
    before = json.dumps({"format": "openrouter-key/1", "revision": str(uuid.uuid4()), "key": key("old")})
    old.write_text(before); old.chmod(0o600)
    chat = ProviderKey(kind="chat", managed_directory=tmp_path, env="develop")
    profiler = ProviderKey(kind="profiler", managed_directory=tmp_path, env="develop")
    for source in (chat, profiler):
        assert source.status()["migrationRequired"]
        with pytest.raises(KeyConflict, match="MIGRATION_REQUIRED"):
            source.save(key(source.kind), None)
    chat.save(key("chat"), None, confirm_migration=True)
    with pytest.raises(KeyUnavailable): profiler.snapshot()
    profiler.save(key("profiler"), None, confirm_migration=True)
    assert chat.snapshot().value != profiler.snapshot().value
    assert old.read_text() == before
    with pytest.raises(KeyUnavailable):
        ProviderKey(kind="chat", managed_directory=tmp_path, env="production").snapshot()
    assert ProviderKey.from_environment("chat", {"PROFILING_OPENROUTER_API_KEY": key("other")}).status()["configured"] is False
    assert ProviderKey.from_environment("profiler", {"OPENROUTER_API_KEY": key("old_common")}).status()["configured"] is False


@pytest.mark.parametrize("kind,variable", [("chat", "OPENROUTER_API_KEY"), ("profiler", "PROFILING_OPENROUTER_API_KEY")])
def test_legacy_aws_reader_rejects_injected_provider_key(tmp_path, kind, variable):
    backend = {"AI_CREDENTIAL_BACKEND": "aws-secrets-manager", "AI_ENVIRONMENT": "develop", f"OPENROUTER_{kind.upper()}_SECRET_ARN": arn(kind)}
    for external in ({variable: key("injected")}, {variable + "_FILE": "/synthetic/not-read"}):
        with pytest.raises(KeyUnavailable, match="SOURCE_CONFLICT"):
            ProviderKey.from_environment(kind, {**backend, **external})


def test_context_snapshot_cannot_cross_service_or_environment(tmp_path):
    chat = ProviderKey(kind="chat", value=key("chat"), env="develop")
    profiler = ProviderKey(kind="profiler", value=key("profiler"), env="develop")
    production = ProviderKey(kind="chat", value=key("production"), env="production")
    token = ACTIVE_KEY.set(chat.snapshot())
    try:
        for source in (profiler, production):
            request = httpx.Request("GET", "https://synthetic.invalid")
            assert ProviderAuth(source).authorize(request).headers["Authorization"] == "Bearer " + source.snapshot().value
    finally:
        ACTIVE_KEY.reset(token)


def test_explicit_legacy_common_directory_is_read_only_and_never_fallback(tmp_path):
    revision = str(uuid.uuid4())
    values = {"openrouter-key.json": ("openrouter-key/1", key("common")),
              "be-service-token.json": ("be-service-token/1", "synthetic_common_token_" * 2)}
    before = {}
    for name, (format_, value) in values.items():
        path = tmp_path / name
        path.write_text(json.dumps({"format": format_, "revision": revision, "key": value}))
        path.chmod(0o600); before[name] = path.read_bytes()
    env = {"AI_CREDENTIAL_LAYOUT": "legacy-shared", "OPENROUTER_MANAGED_KEY_DIR": str(tmp_path),
           "BE_SERVICE_TOKEN_DIR": str(tmp_path)}
    for kind in ("chat", "profiler"):
        provider = ProviderKey.from_environment(kind, env)
        backend = ServiceToken.from_environment(kind, env)
        for source, name in ((provider, "openrouter-key.json"), (backend, "be-service-token.json")):
            assert source.snapshot().value == values[name][1]
            assert source.snapshot().revision == revision
            assert source.status()["configured"] and not source.status()["writable"]
            with pytest.raises(KeyConflict, match="OPERATOR_MANAGED"):
                source.save(values[name][1], revision)
        # Without explicit selection, /1 remains a migration requirement.
        with pytest.raises(KeyUnavailable, match="MIGRATION_REQUIRED"):
            ProviderKey(kind=kind, managed_directory=tmp_path).snapshot()
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    with pytest.raises(KeyUnavailable, match="SOURCE_CONFLICT"):
        ProviderKey.from_environment('profiler', {**env, 'OPENROUTER_API_KEY': key('separate_chat')})
    # A separated credential arriving after startup cannot silently share /1.
    separated = tmp_path / "openrouter-chat-key.json"
    separated.write_text("corrupt"); separated.chmod(0o600)
    with pytest.raises(KeyUnavailable, match="LAYOUT_CONFLICT"):
        provider.snapshot()
    with pytest.raises(KeyUnavailable):
        ProviderKey(kind="profiler", managed_directory=tmp_path).snapshot()


@pytest.mark.parametrize("field,value", [("format", "openrouter-key/2"), ("revision", "bad"), ("key", "bad")])
def test_legacy_compatibility_validates_record_and_environment(tmp_path, field, value):
    record = {"format": "openrouter-key/1", "revision": str(uuid.uuid4()), "key": key("common")}
    record[field] = value
    path = tmp_path / "openrouter-key.json"; path.write_text(json.dumps(record)); path.chmod(0o600)
    with pytest.raises(KeyUnavailable):
        ProviderKey(managed_directory=tmp_path, layout="legacy-shared").snapshot()
    with pytest.raises(KeyUnavailable, match="LOCAL_MANAGED"):
        ProviderKey(managed_directory=tmp_path, layout="legacy-shared", env="production")


def test_legacy_be_files_require_explicit_shared_transition_and_survive(tmp_path):
    old = tmp_path / "be-service-token.json"
    before = '{"format":"be-service-token/1","key":"SYNTHETIC_OLD_TOKEN"}'
    old.write_text(before); old.chmod(0o600)
    source = ServiceToken("chat", managed_directory=tmp_path)
    with pytest.raises(KeyConflict): source.save("synthetic_common_token_" * 2, None)
    saved = source.save("synthetic_common_token_" * 2, None, confirm_shared=True)
    assert ServiceToken("profiler", managed_directory=tmp_path).snapshot().revision == saved["revision"]
    assert old.read_text() == before


def test_profiler_concurrent_requests_keep_their_selected_header(tmp_path):
    from integration.profiler_boundary import ProviderLimitedExtractor
    source = ProviderKey(kind="profiler", managed_directory=tmp_path)
    old, new = key("before"), key("after")
    saved = source.save(old, None)
    entered, release, wire = Event(), Event(), []
    def send(request):
        selected = request.headers["authorization"]
        wire.append(selected)
        if selected == "Bearer " + old:
            entered.set(); assert release.wait(3)
            assert request.headers["authorization"] == selected
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
    extractor = ProviderLimitedExtractor("https://openrouter.ai/api/v1", "", "synthetic/model",
                                         credential_source=source, transport=httpx.MockTransport(send))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(extractor.extract, "synthetic", "synthetic", {})
            try:
                assert entered.wait(3)
                source.save(new, saved["revision"])
                pool.submit(extractor.extract, "synthetic", "synthetic", {}).result(timeout=3)
            finally: release.set()
            first.result(timeout=3)
    finally: extractor.close()
    assert wire == ["Bearer " + old, "Bearer " + new]


@pytest.mark.parametrize("status", [200, 402, 429])
def test_profiler_reads_own_key_each_request_and_recovers_after_missing(tmp_path, status):
    from integration.profiler_boundary import ProviderLimitedExtractor
    from profiling.ports import TagExtractError
    chat = ProviderKey(kind="chat", managed_directory=tmp_path); chat.save(key("chat"), None)
    source = ProviderKey(kind="profiler", managed_directory=tmp_path)
    wire = []
    def send(request):
        wire.append(request.headers["authorization"])
        return httpx.Response(status, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                                           "usage": {"cost": 0.001}, "error": key("private_echo")})
    extractor = ProviderLimitedExtractor("https://openrouter.ai/api/v1", key("unused"), "synthetic/model",
                                         credential_source=source, transport=httpx.MockTransport(send))
    try:
        with pytest.raises(TagExtractError): extractor.extract("synthetic", "synthetic", {})
        assert not wire
        saved = source.save(key("profiler_first"), None)
        for value in (key("profiler_first"), key("profiler_next")):
            if value != key("profiler_first"):
                source.save(value, saved["revision"])
            if status == 200:
                extractor.extract("synthetic", "synthetic", {})
            else:
                with pytest.raises(TagExtractError) as exc: extractor.extract("synthetic", "synthetic", {})
                assert key("private_echo") not in str(exc.value)
            assert wire[-1] == "Bearer " + value
        assert len(wire) == 2 and all(key("chat") not in item for item in wire)
    finally:
        extractor.close()


def test_common_be_token_rotates_intake_profile_read_and_callback(tmp_path, monkeypatch):
    from profiling import intake, profile_read
    from profiling.backend import HttpBackendPort
    from profiling.types import ProfileOutcome, RunStatus, SearchResult
    from profiling.settings import Settings
    source = ServiceToken("profiler", managed_directory=tmp_path)
    old, new = "synthetic_BE_first_" * 2, "synthetic_BE_next_" * 2
    saved = source.save(old, None)
    app = FastAPI(); app.state.be_token_provider = source.snapshot; app.state.engine = object()
    app.dependency_overrides[intake.settings_dep] = lambda: Settings(_env_file=None, SERVICE_TOKEN="synthetic_legacy_unused")
    app.get("/intake-auth", dependencies=[Depends(intake.require_service_token)])(lambda: {"ok": True})
    app.include_router(profile_read.router)
    monkeypatch.setattr(profile_read, "read_profile", lambda *a: {"available": True})
    wire = []
    backend = HttpBackendPort("https://synthetic.invalid", "synthetic_legacy_unused", 1,
                              token_provider=source.snapshot,
                              transport=httpx.MockTransport(lambda request: wire.append(request.headers["authorization"]) or httpx.Response(200)))
    outcome = ProfileOutcome(recipient_user_id=42, source_version=1, status=RunStatus.RESULT_READY,
                             search=SearchResult(product_ids=[101], query_text="", catalog_version_id=uuid.UUID(int=1)))
    with TestClient(app) as client:
        body = {"grantId": str(uuid.UUID(int=1)), "callerUserId": 7, "recipientUserId": 42, "sourceVersion": 1}
        for value in (old, new):
            if value == new: source.save(new, saved["revision"])
            assert client.get("/intake-auth", headers={"Authorization": "Bearer " + value}).status_code == 200
            assert client.post("/internal/v1/profiles/read", json=body, headers={"Authorization": "Bearer " + value}).status_code == 200
            assert client.get("/intake-auth", headers={"Authorization": "Bearer synthetic_legacy_unused"}).status_code == 401
            assert backend.send_profile_callback(outcome).status == RunStatus.DELIVERED
        assert client.get("/intake-auth", headers={"Authorization": "Bearer " + old}).status_code == 401
        assert client.post("/internal/v1/profiles/read", json=body, headers={"Authorization": "Bearer " + old}).status_code == 401
        (tmp_path / source.record_file).unlink()
        assert client.get("/intake-auth", headers={"Authorization": "Bearer " + new}).status_code == 503
        assert client.post("/internal/v1/profiles/read", json=body, headers={"Authorization": "Bearer " + new}).status_code == 503
        assert backend.send_profile_callback(outcome).code.value == "CALLBACK_UNREACHABLE"
    backend.close()
    assert wire == ["Bearer " + old, "Bearer " + new]


def test_refresh_uses_stable_internal_secretstr_not_shared_be_token():
    from integration.profiler_rebind import require_refresh_token
    from fastapi import HTTPException
    from profiling.settings import Settings
    settings = Settings(_env_file=None, SERVICE_TOKEN="", SEARCH_TOKEN="synthetic-internal")
    asyncio.run(require_refresh_token(settings, "Bearer synthetic-internal"))
    with pytest.raises(HTTPException): asyncio.run(require_refresh_token(settings, "Bearer synthetic-be-token"))


def test_profiler_runtime_credential_observation_requires_current_common_token(tmp_path, monkeypatch):
    from integration.profiler_runtime import credential_status
    monkeypatch.setenv("BE_SERVICE_TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("AI_ENVIRONMENT", "develop")
    source = ProviderKey(kind="profiler", managed_directory=tmp_path, env="develop")
    saved = source.save(key("profiler"), None)
    token = "synthetic_common_token_" * 2
    ServiceToken.from_environment("profiler").save(token, None)
    app = FastAPI(); app.state.provider_key = source
    app.get("/internal/credentials")(credential_status)
    with TestClient(app) as client:
        assert client.get("/internal/credentials").status_code == 401
        response = client.get("/internal/credentials", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        assert response.json()["service"] == "profiler" and response.json()["environment"] == "develop"
        assert response.json()["provider"]["revision"] == saved["revision"]
        assert key("profiler") not in response.text and token not in response.text


def test_healthcheck_uses_original_explicit_internal_environment(tmp_path, monkeypatch):
    import io
    import sys
    from integration.containers import health
    monkeypatch.setenv("BE_SERVICE_TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("AI_INTERNAL_SERVICE_TOKEN", "synthetic-internal")
    token = "synthetic_common_token_" * 2
    ServiceToken.from_environment("chat").save(token, None)
    monkeypatch.setattr(sys, "argv", ["health", "chat"])
    seen = []
    def send(request, **kw):
        seen.append(request.get_header("Authorization"))
        return io.BytesIO(b'{"ready":true}')
    monkeypatch.setattr(health.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=send))
    # No configure() call: model Docker exec's original environment.
    health.main()
    assert seen == ["Bearer " + token]


def arn(kind):
    return "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:develop/" + kind + "-abcdef"


class FakeSecrets:
    def __init__(self):
        self.values, self.current, self.labels = {}, {}, {}
        self.lock = Lock(); self.barrier = None; self.fault = None; self.writes = 0

    def provision(self, source):
        version = str(uuid.uuid4())
        self.values[source.aws_secret_id, version] = json.dumps(source.record(None, version))
        self.current[source.aws_secret_id] = version
        return version

    def get_secret_value(self, *, SecretId, VersionStage):
        assert VersionStage == "AWSCURRENT"
        if self.fault == "read": raise RuntimeError(key("private_aws_error"))
        with self.lock:
            version = self.current[SecretId]
            return {"SecretString": self.values[SecretId, version], "VersionId": version, "VersionStages": ["AWSCURRENT"]}

    def put_secret_value(self, *, SecretId, ClientRequestToken, VersionStages, SecretString):
        assert len(VersionStages) == 1 and VersionStages[0].startswith("console-")
        self.writes += 1
        if self.fault == "put": raise RuntimeError(key("private_aws_error"))
        with self.lock:
            assert SecretId in self.current  # Never auto-create the first version.
            self.values[SecretId, ClientRequestToken] = SecretString
            self.labels[SecretId, VersionStages[0]] = ClientRequestToken
        if self.barrier: self.barrier.wait(timeout=3)

    def update_secret_version_stage(self, *, SecretId, VersionStage, RemoveFromVersionId, MoveToVersionId=None):
        with self.lock:
            if VersionStage != "AWSCURRENT":
                self.labels.pop((SecretId, VersionStage), None); return
            assert self.current[SecretId] == RemoveFromVersionId, "CAS conflict"
            if self.fault == "promote": raise RuntimeError(key("private_aws_error"))
            self.current[SecretId] = MoveToVersionId
            if self.fault == "after_promote": raise TimeoutError(key("private_aws_error"))


def aws_source(store, kind="chat", env="develop"):
    return ProviderKey(kind=kind, env=env, aws_secret_id=arn(kind), aws_client=store)


def test_legacy_aws_read_only_restart_and_environment_binding():
    store = FakeSecrets(); source = aws_source(store)
    assert not source.status()["writable"]
    with pytest.raises(KeyUnavailable): source.save(key("chat"), None)
    assert store.writes == 0
    revision = store.provision(source)
    assert source.status()["revision"] == revision and not source.status()["writable"]
    store.values[source.aws_secret_id, revision] = json.dumps(source.record(key("chat"), revision))
    assert aws_source(store).snapshot().value == key("chat")
    with pytest.raises(KeyConflict, match="OPERATOR_MANAGED"):
        source.save(key("replacement"), revision)
    assert store.writes == 0 and not store.labels
    with pytest.raises(KeyUnavailable): aws_source(store, env="production").snapshot()
    with pytest.raises(KeyUnavailable): aws_source(store, kind="profiler").snapshot()
    assert key("chat") not in json.dumps(source.status())


def test_aws_read_failure_never_echoes_values_or_permits_writes():
    store = FakeSecrets(); source = aws_source(store); revision = store.provision(source)
    store.fault = "read"
    with pytest.raises(KeyUnavailable) as error: source.snapshot()
    assert "synthetic" not in str(error.value)
    with pytest.raises(KeyConflict): source.save(key("next"), revision)
    assert not store.labels and store.writes == 0


def test_single_team_secret_is_never_replaced():
    store = FakeSecrets(); source = aws_source(store); revision = store.provision(source)
    team = {"OPENROUTER_API_KEY": key("chat"), "PROFILING_OPENROUTER_API_KEY": key("profiler"),
            "BE_SERVICE_TOKEN": "synthetic_common_" * 3, "OTHER_TEAM_SETTING": "synthetic_preserve"}
    before = json.dumps(team)
    store.values[source.aws_secret_id, revision] = before
    with pytest.raises(KeyUnavailable): source.snapshot()
    with pytest.raises(KeyConflict): source.save(key("next"), revision)
    assert not source.status()["writable"] and store.writes == 0
    assert store.values[source.aws_secret_id, revision] == before
