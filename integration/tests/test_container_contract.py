"""Offline image/configuration contracts; these are NOT image build/start evidence."""

import hashlib
import json
import sys
import os
from pathlib import Path

import httpx
import pytest
import yaml

from integration.containers import entrypoint
from integration.containers.relay import ACCOUNT, BudgetRelay, validate_summary

ROOT = Path(__file__).resolve().parents[2]
CONTAINERS = ROOT / "integration/containers"


def summary():
    return {
        "limitUsd": 5,
        "halted": None,
        "remainingUsd": 4,
        "validationMaxAttempts": 6,
        "validationMaxReservedUsd": 0.2,
        "validationAttempts": 0,
        "validationReservedUsd": 0,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("halted", "halt"),
        ("limitUsd", 10),
        ("remainingUsd", 0),
        ("remainingUsd", float("nan")),
        ("validationMaxAttempts", 5),
        ("validationMaxAttempts", True),
        ("validationMaxReservedUsd", 1),
        ("validationAttempts", 6),
        ("validationAttempts", -1),
        ("validationReservedUsd", 0.2),
        ("validationReservedUsd", float("inf")),
    ],
)
def test_bad_fence_fails_closed(field, value):
    data = summary()
    data[field] = value
    with pytest.raises(ValueError):
        validate_summary(data, 6, 0.2)


@pytest.mark.parametrize("field", list(summary()))
def test_missing_fence_fields_fail_closed(field):
    data = summary()
    del data[field]
    with pytest.raises(ValueError):
        validate_summary(data, 6, 0.2)


def test_guarded_compose_separate_processes_and_one_external_database():
    cfg = yaml.safe_load((CONTAINERS / "compose.guarded.yaml").read_text())
    services = cfg["services"]
    assert [
        s for s, v in services.items() if v.get("image", "").startswith("pgvector/pgvector:")
    ] == ["postgres"]
    assert cfg["volumes"]["postgres-data"]["external"] is True
    assert cfg["networks"]["backend"]["internal"] is True
    assert set(services["mesh"]["networks"]) == {"backend", "egress"}
    assert services["mesh"]["ports"] == [
        "127.0.0.1:${INTEGRATION_CHAT_PORT:-18860}:7860",
        "127.0.0.1:${INTEGRATION_PROFILER_PORT:-18861}:8001"
    ]
    for role in ["chat", "search", "profiler"]:
        service = services[role]
        assert service["network_mode"] == "service:mesh" and service["command"] == [
            role
        ]
        assert (
            service["read_only"]
            and not service.get("ports")
            and service["cap_drop"] == ["ALL"]
        )
        assert "extra_hosts" not in service
    for role in ["chat", "profiler"]:
        env = services[role]["environment"]
        assert env["BUDGET_GUARDED_ORIGIN"].startswith("${BUDGET_GUARDED_ORIGIN:?")
        assert env["OPENROUTER_API_KEY_FILE"] == "/run/secrets/openrouter_key"
        assert "OPENROUTER_API_KEY" not in env
    for role in ["search"]:
        mount = next(
            m
            for m in services[role]["volumes"]
            if isinstance(m, dict) and m["target"] == "/assets/models"
        )
        assert mount["read_only"] and not mount["bind"]["create_host_path"]
    assert not services["postgres"].get("ports")
    assert "POSTGRES_HOST_AUTH_METHOD" not in services["postgres"]["environment"]


def test_smoke_has_no_egress_or_external_secrets_and_faults():
    cfg = yaml.safe_load((CONTAINERS / "compose.smoke.yaml").read_text())
    assert cfg["networks"] == {"backend": {"internal": True}}
    assert cfg["services"]["mesh"]["networks"] == ["backend"]
    assert "secrets" not in cfg
    assert (
        cfg["services"]["qa-init"]["environment"]["INTEGRATION_SYNTHETIC_ONLY"] == "1"
    )
    for fault in ["missing-budget", "unready-db"]:
        service = cfg["services"]["fault-" + fault]
        assert service["profiles"] == ["faults"] and service["command"] == [
            "profiler-smoke"
        ]
    asset_fault = cfg["services"]["fault-missing-assets"]
    assert asset_fault["profiles"] == ["faults"]
    assert asset_fault["command"] == ["search"] and asset_fault["build"]["target"] == "search"
    assert asset_fault["volumes"] == ["smoke-data:/run/fixture:ro"]
    assert cfg["services"]["chat"]["command"] == ["chat-smoke"]
    assert "OPENROUTER_API_KEY_FILE" not in cfg["services"]["chat"]["environment"]


def test_images_use_dedicated_entrypoint_and_no_asset_download_or_broad_copy():
    text = (CONTAINERS / "Dockerfile").read_text()
    assert (
        "COPY . " not in text
        and "download_model" not in text
        and "profiling.main:app" not in text
    )
    assert (
        text.count('ENTRYPOINT ["python","-m","integration.containers.entrypoint"]')
        == 4
    )
    production = text.split("# Synthetic-only image variants;")[0]
    assert (
        "COPY integration/tests/" not in production
        and "touch /workspace/integration/containers/smoke_only" not in production
    )
    assert "require('onnxruntime-node')" in text
    assert "COPY integration/*.py" not in production
    assert "integration/live_backend.py" not in production
    ignore = (CONTAINERS / "Dockerfile.dockerignore").read_text()
    assert ignore.startswith("**\n")
    for item in [
        "**/.env*",
        "**/models/**",
        "**/node_modules/**",
        "**/returned/**",
        "**/*.db",
    ]:
        assert item in ignore


@pytest.mark.parametrize("role", ["profiler", "chat", "search", "console"])
def test_database_cannot_point_to_original_host_or_database(role):
    with pytest.raises(ValueError):
        entrypoint.database("postgresql://fixture_owner@127.0.0.1/run2", role)


def test_missing_budget_prevents_assets_or_database_and_provider(monkeypatch):
    for name in [
        "BUDGET_GUARDED_ORIGIN",
        "BUDGET_EXPECTED_MAX_ATTEMPTS",
        "BUDGET_EXPECTED_MAX_RESERVED_USD",
    ]:
        monkeypatch.delenv(name, raising=False)

    def fake_secret(name, **kwargs):
        return (
            "postgresql+psycopg://ai_app@postgres/emmet4930_test"
            if name == "PROFILING_DATABASE_URL"
            else "synthetic-unit-only"
        )

    monkeypatch.setattr(entrypoint, "secret", fake_secret)

    def forbidden(*args):
        pytest.fail("Missing budget must block before assets/DB/provider")

    monkeypatch.setattr(entrypoint, "prepare_encoder", forbidden)
    with pytest.raises((KeyError, ValueError)):
        entrypoint.configure("profiler")


def test_missing_and_mismatching_assets_fail_before_database(tmp_path, monkeypatch):
    code = tmp_path / "code"
    code.mkdir()
    assets = tmp_path / "assets"
    (assets / "models").mkdir(parents=True)
    (code / "model.lock.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "path": "model",
                        "bytes": 3,
                        "sha256": hashlib.sha256(b"abc").hexdigest(),
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("ENCODER_CODE_ROOT", str(code))
    with pytest.raises(ValueError, match="missing or wrong size"):
        entrypoint.prepare_encoder(tmp_path / "runtime", assets)
    (assets / "models/model").write_bytes(b"bad")
    with pytest.raises(ValueError, match="hash mismatch"):
        entrypoint.prepare_encoder(tmp_path / "runtime", assets)


@pytest.mark.parametrize('changed_code', [False, True])
def test_sync_preparation_never_rewrites_shared_encoder_code(tmp_path, monkeypatch, changed_code):
    code=tmp_path/'code';code.mkdir();(code/'node_modules').mkdir()
    assets=tmp_path/'assets';(assets/'models').mkdir(parents=True)
    (assets/'models/model').write_bytes(b'abc')
    (code/'model.lock.json').write_text(json.dumps({'files':[
        {'path':'model','bytes':3,'sha256':hashlib.sha256(b'abc').hexdigest()}]}))
    for name in ('service.mjs','runtime.mjs','documents.mjs'):
        (code/name).write_text('// synthetic version 1')
    monkeypatch.setenv('ENCODER_CODE_ROOT',str(code))
    runtime=tmp_path/'runtime';entrypoint.prepare_encoder(runtime,assets)
    prepared=runtime/'embedding/service.mjs';before=prepared.stat().st_mtime_ns
    prepared.chmod(0o444)
    if changed_code:
        (code/'service.mjs').write_text('// synthetic version 2')
        with pytest.raises(ValueError,match='code binding changed'):
            entrypoint.prepare_encoder(runtime,assets)
    else:
        entrypoint.prepare_encoder(runtime,assets)
    assert prepared.stat().st_mtime_ns==before
    assert prepared.read_text()=='// synthetic version 1'


def test_unready_database_propagates_without_starting_relay(monkeypatch):
    monkeypatch.setenv("AI_DATABASE_NAME", "emmet4930_test")
    from integration import database_identity
    monkeypatch.setattr(database_identity, "audit", lambda *a: None)
    monkeypatch.setenv("PROFILING_APP_DB_ROLE", "ai_profiler_service")
    monkeypatch.setattr(
        entrypoint,
        "secret",
        lambda name, **kw: (
            "postgresql+psycopg://ai_profiler_service@postgres/emmet4930_test"
            if name == "PROFILING_DATABASE_URL"
            else "synthetic-unit-only"
        ),
    )
    monkeypatch.setattr(entrypoint, "prepare_encoder", lambda *args: None)
    monkeypatch.syspath_prepend(str(ROOT / "product-search"))
    import catalog_db

    def unavailable(*args):
        raise RuntimeError("SYNTHETIC database unavailable")

    monkeypatch.setattr(entrypoint, "profile_database", unavailable)
    relay = BudgetRelay(
        "http://host.docker.internal:19999",
        "synthetic",
        6,
        0.2,
        transport=httpx.MockTransport(
            lambda r: pytest.fail("DB failure must precede relay/provider calls")
        ),
    )
    monkeypatch.setattr(
        BudgetRelay, "from_environment", classmethod(lambda cls, *args, **kwargs: relay)
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        entrypoint.configure("profiler")
    assert relay.http.is_closed and relay.server is None


@pytest.mark.parametrize('ready', [False, True])
def test_profile_database_requires_readonly_role_audit(monkeypatch, ready):
    from integration import audit_profile_database
    calls = []
    def audit(url):
        calls.append(url)
        return {'ready': ready, 'changed': False}
    monkeypatch.setattr(audit_profile_database, 'audit', audit)
    if ready:
        assert entrypoint.profile_database('synthetic-url') is None
    else:
        with pytest.raises(ValueError, match='no automatic repair'):
            entrypoint.profile_database('synthetic-url')
    assert calls == ['synthetic-url']


def test_only_fixed_roles_allowed():
    with pytest.raises(ValueError):
        entrypoint.configure("profiling.main:app")
    with pytest.raises(ValueError, match="absent"):
        entrypoint.configure("profiler-smoke")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:19999",
        "http://example.invalid:19999",
        "http://host.docker.internal",
        "http://host.docker.internal:19999/rpc",
        "http://user@host.docker.internal:19999",
    ],
)
def test_normal_relay_rejects_unapproved_origin(url):
    with pytest.raises(ValueError):
        BudgetRelay(url, "synthetic", 6, 0.2)


def test_relay_only_cost_methods_and_finite_settlement():
    requests = []

    def fake(request):
        data = json.loads(request.content)
        requests.append(data)
        return httpx.Response(
            200,
            json={"result": summary() if data["method"] == "budget_summary" else True},
        )

    relay = BudgetRelay(
        "http://host.docker.internal:19999",
        "synthetic",
        6,
        0.2,
        transport=httpx.MockTransport(fake),
    )
    try:
        relay.summary()
        with pytest.raises(ValueError):
            relay.forward({"method": "start", "args": {"account": ACCOUNT}})
        with pytest.raises(ValueError):
            relay.forward(
                {
                    "method": "budget_finish",
                    "args": {
                        "account": ACCOUNT,
                        "ident": "test",
                        "cost": float("nan"),
                        "status": "received",
                    },
                }
            )
        assert len(requests) == 1
        assert relay.forward(
            {
                "method": "budget_finish",
                "args": {
                    "account": ACCOUNT,
                    "ident": "test",
                    "cost": None,
                    "status": "unknown",
                },
            }
        ) == {"result": True}
    finally:
        relay.close()


def test_redirect_never_followed():
    calls = []

    def redirect(request):
        calls.append(request)
        return httpx.Response(
            307, headers={"Location": "https://example.invalid/never"}
        )

    relay = BudgetRelay(
        "http://host.docker.internal:19999",
        "synthetic",
        6,
        0.2,
        transport=httpx.MockTransport(redirect),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            relay.summary()
        assert len(calls) == 1
    finally:
        relay.close()


def test_console_config_written_before_app_module_import(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_DATABASE_NAME", "emmet4930_candidate")
    from integration import database_identity
    monkeypatch.setattr(database_identity, "audit", lambda *a: None)
    import builtins

    import psycopg

    real_import = builtins.__import__

    def import_guard(name, *args, **kwargs):
        if name == "console.server":
            pytest.fail("Import would cache an unconfigured module-level app")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_guard)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, *args):
            return self

        def fetchone(self):
            return ("00000000-0000-4000-8000-000000000001",)

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kw: Connection())
    real_client = httpx.Client

    def client(**kwargs):
        return real_client(
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"snapshotId": "synthetic-snapshot", "catalogVersion": "00000000-0000-4000-8000-000000000001"}
                )
            ),
        )

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(
        entrypoint,
        "secret",
        lambda name, **kw: (
            "postgresql://ai_console_service@postgres/emmet4930_candidate"
            if name == "DATABASE_URL"
            else "synthetic-only"
        ),
    )
    real_path = Path
    target = tmp_path / "targets.json"
    monkeypatch.setattr(
        entrypoint,
        "Path",
        lambda value: (
            target if value == "/run/runtime/targets.json" else real_path(value)
        ),
    )
    assert entrypoint.configure("console") is None
    config = json.loads(target.read_text())
    assert (
        config["searchSnapshotId"] == "synthetic-snapshot"
        and config["liveModel"] is True
    )
    assert target.stat().st_mode & 0o777 == 0o600
    assert entrypoint.os.environ["CONSOLE_TARGET_FILE"] == str(target)


def test_chat_role_uses_migrated_schema_without_public_create(monkeypatch):
    monkeypatch.setenv("AI_DATABASE_NAME", "emmet4930_candidate")
    from integration import database_identity
    monkeypatch.setattr(database_identity, "audit", lambda *a: None)
    from urllib.parse import parse_qs, urlsplit

    value = entrypoint.database(
        "postgresql://ai_chat_service@postgres/emmet4930_candidate?sslmode=disable",
        "chat",
    )
    query = parse_qs(urlsplit(value).query)
    assert query["options"] == ["-csearch_path=ai_chat,public"] and query[
        "sslmode"
    ] == ["disable"]
    dockerfile = (CONTAINERS / "Dockerfile").read_text()
    assert "app.build_assets() writes only absent files" in dockerfile
    assert "target=root/'assets'/version" in dockerfile


@pytest.mark.parametrize("name", ["compose.guarded.yaml", "compose.smoke.yaml"])
def test_shared_network_joiners_do_not_override_donor_hosts_or_dns(name):
    cfg = yaml.safe_load((CONTAINERS / name).read_text())
    mesh = cfg["services"]["mesh"]
    assert "backend" in mesh["networks"] and cfg["networks"]["backend"]["internal"]
    if name == "compose.guarded.yaml":
        assert mesh["extra_hosts"] == ["host.docker.internal:host-gateway"]
    forbidden = {
        "extra_hosts",
        "dns",
        "dns_search",
        "dns_opt",
        "hostname",
        "domainname",
        "ports",
        "networks",
    }

    def validate(service):
        assert service["network_mode"] == "service:mesh"
        assert not (forbidden & service.keys())

    for service in cfg["services"].values():
        if service.get("network_mode") == "service:mesh":
            validate(service)
            # This must reject the independently reported pre-fix combination.
            with pytest.raises(AssertionError):
                validate(
                    service | {"extra_hosts": ["host.docker.internal:host-gateway"]}
                )
    assert cfg["services"]["search"]["build"]["target"] == "search"
    assert cfg["services"]["search"]["environment"]["SEARCH_CATALOG_DATABASE_URL_FILE"]


def test_unmodified_search_import_and_static_assets_with_readonly_source():
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(ROOT / "integration/tests/verify_container_readonly.py"),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=50,
    )
    proof = json.loads(result.stdout)
    assert proof["passed"] and proof["preFixPermissionErrorReproduced"]
    assert proof["postFixImportAndBuildAssets"]["sourceRuntimeEmpty"]
    assert proof["imagesBuilt"] is False and proof["realDatabaseCalls"] == 0


def test_catalog_sync_is_an_explicit_separate_search_job():
    config = yaml.safe_load((CONTAINERS / 'compose.sync.yaml').read_text())
    worker = config['services']['catalog-sync']
    assert worker['build']['target'] == 'search'
    assert worker['entrypoint'] == ['python', '-m', 'integration.search_sync']
    assert worker['profiles'] == ['sync'] and worker['restart'] == 'no'
    assert worker['network_mode'] == 'service:mesh' and 'ports' not in worker
    assert 'sync-cache:/runtime' in worker['volumes']
    assert worker['environment']['CATALOG_SYNC_DATABASE_URL_FILE']=='/run/secrets/catalog_sync_database_url'
    normal = yaml.safe_load((CONTAINERS / 'compose.guarded.yaml').read_text())
    assert 'SEARCH_CATALOG_MODE' not in normal['services']['search']['environment']
    assert normal['services']['search']['environment']['AI_DATABASE_NAME'].startswith('${AI_DATABASE_NAME:?')
    assert 'fixture_owner' not in str(normal) and 'console' not in normal['services'] and 'backend' not in normal['services']
    assert not normal['services']['profiler'].get('volumes')
    assert 'rabbit' not in str(normal).lower()


@pytest.mark.parametrize("name", ["compose.guarded.yaml", "compose.smoke.yaml"])
def test_postgres_image_has_verified_vector_extension_and_existing_volume_contract(name):
    cfg = yaml.safe_load((CONTAINERS / name).read_text())
    postgres = cfg["services"]["postgres"]
    assert postgres["image"] == (
        "pgvector/pgvector:0.8.7-pg18-bookworm@sha256:"
        "2358fcba361ed2233a5ed81b5fe4ca779ccb304120ce531a3bf51c0ed7e2bc11"
    )
    assert postgres["environment"]["PGDATA"] == "/var/lib/postgresql/data/pgdata"
    assert not postgres.get("ports")
    if name == "compose.guarded.yaml":
        assert cfg["volumes"]["postgres-data"]["external"] is True
        assert postgres["volumes"] == ["postgres-data:/var/lib/postgresql/data"]
        assert "POSTGRES_HOST_AUTH_METHOD" not in postgres["environment"]


def test_smoke_profiler_depends_on_search_and_does_not_own_encoder_assets():
    cfg = yaml.safe_load((CONTAINERS / "compose.smoke.yaml").read_text())
    service = cfg["services"]["profiler"]
    assert service["depends_on"]["search"] == {"condition": "service_healthy"}
    assert service["volumes"] == ["smoke-data:/run/fixture:ro"]
    assert "profiler-cache" not in cfg["volumes"]


@pytest.mark.parametrize("role", ["profiler", "profiler-smoke"])
def test_both_profiler_launchers_use_shared_search_without_local_vectors(role, tmp_path, monkeypatch):
    monkeypatch.setenv("AI_DATABASE_NAME", "emmet4930_test")
    from integration import database_identity
    monkeypatch.setattr(database_identity, "audit", lambda *a: None)
    marker = tmp_path / "synthetic-marker"
    marker.touch()
    real_path = Path
    monkeypatch.setattr(entrypoint, "Path", lambda value: marker if value in {
        "/workspace/integration/containers/smoke_only",
        "/workspace/integration/tests/emmet_fixture_profiler.py",
    } else real_path(value))
    monkeypatch.setenv("PROFILING_APP_DB_ROLE", "ai_profiler_service")
    # Ensure all launcher environment changes are restored even after a failure.
    monkeypatch.setattr(entrypoint.os, "environ", os.environ.copy())
    monkeypatch.setattr(entrypoint, "secret", lambda name, **kw:
        "postgresql+psycopg://ai_profiler_service@postgres/emmet4930_test"
        if name == "PROFILING_DATABASE_URL" else "synthetic-launch-test")
    audits = []
    monkeypatch.setattr(entrypoint, "profile_database", lambda value: audits.append(value))
    monkeypatch.setattr(entrypoint, "prepare_encoder", lambda *args:
        pytest.fail("Profiler must use Search, not load or materialize local vectors"))

    class Relay:
        url = "http://127.0.0.1:19000"
        started = False
        def start(self): self.started = True
        def close(self): pass

    relay = Relay()
    modes = []
    def factory(cls, token, *, synthetic):
        modes.append(synthetic)
        return relay
    monkeypatch.setattr(BudgetRelay, "from_environment", classmethod(factory))
    assert entrypoint.configure(role) is relay
    assert relay.started and len(audits) == 1
    assert modes == [role.endswith("-smoke")]
    assert os.environ["PROFILING_CATALOG_SOURCE"] == "search_http"
    assert os.environ["PROFILING_SEARCH_BASE_URL"] == "http://127.0.0.1:8000"
    assert os.environ["PROFILING_SEARCH_TOKEN"] == os.environ["AI_SERVICE_TOKEN"]
    assert os.environ["PROFILING_TAG_SPEC"] == "tag3disliked"
