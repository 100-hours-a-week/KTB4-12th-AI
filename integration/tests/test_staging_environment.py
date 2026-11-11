"""Staging identity contracts with synthetic credentials and no external calls."""
import json
import os
from pathlib import Path
from unittest.mock import patch
import uuid

import httpx
import pytest

from integration import database_identity
from integration.containers import entrypoint
from integration.provider_key import ACTIVE_KEY, KeyUnavailable, ProviderAuth, ProviderKey, environment
from integration.service_token import ServiceToken

DEPLOYMENTS = ("develop", "staging", "production")
CREDENTIALS = ("provider-chat", "provider-profiler", "service-chat", "service-profiler")


@pytest.fixture(autouse=True)
def isolated_environment():
    with patch.dict(os.environ, {}, clear=True):
        yield


def value(credential, label="saved"):
    prefix = "sk-or-v1-" if credential.startswith("provider-") else ""
    return prefix + ("synthetic_" + credential + "_" + label + "_") * 2


def source(credential, deployment="staging", **kwargs):
    purpose, kind = credential.split("-")
    cls = ProviderKey if purpose == "provider" else ServiceToken
    return cls(kind=kind, env=deployment, **kwargs)


@pytest.mark.parametrize("deployment", ("local", *DEPLOYMENTS))
def test_exact_environment_names_remain_distinct(deployment):
    assert environment({"AI_ENVIRONMENT": deployment}) == deployment
    assert environment({}) == "local"


@pytest.mark.parametrize("invalid", ("stage", "Staging", " staging ", "", "prod", None))
@pytest.mark.parametrize("credential", CREDENTIALS)
def test_invalid_environment_fails_closed(credential, invalid):
    with pytest.raises(KeyUnavailable, match="CREDENTIAL_ENVIRONMENT_INVALID"):
        source(credential, invalid, value=value(credential))


@pytest.mark.parametrize("credential", CREDENTIALS)
def test_staging_injected_credentials_keep_exact_environment(credential):
    purpose, kind = credential.split("-")
    variable = f"OPENROUTER_API_KEY_{kind.upper()}" if purpose == "provider" else "BE_SERVICE_TOKEN"
    cls = ProviderKey if purpose == "provider" else ServiceToken
    selected = cls.from_environment(kind, {"AI_ENVIRONMENT": "staging", variable: value(credential)})
    assert selected.environment == "staging"
    assert selected.snapshot().value == value(credential)
    assert selected.snapshot().scope[0] == "staging"


@pytest.mark.parametrize("credential", CREDENTIALS)
def test_staging_managed_records_restart_and_never_fall_back(credential, tmp_path):
    tmp_path.chmod(0o700)
    current = source(credential, managed_directory=tmp_path, value=value(credential, "seed"))
    saved = current.save(value(credential), None)
    record_path = tmp_path / current.record_file
    record = json.loads(record_path.read_text())
    assert record["environment"] == "staging" and record["purpose"] == current.purpose
    reader = source(credential, managed_directory=tmp_path, value=value(credential, "stale"))
    assert reader.snapshot().revision == saved["revision"]
    assert reader.snapshot().value == value(credential)
    record_path.write_text("corrupt")
    with pytest.raises(KeyUnavailable):
        reader.snapshot()
    record_path.unlink()
    with pytest.raises(KeyUnavailable):
        reader.snapshot()


@pytest.mark.parametrize("credential", CREDENTIALS)
@pytest.mark.parametrize("stored,requested", [(a, b) for a in ("local", *DEPLOYMENTS)
                                             for b in ("local", *DEPLOYMENTS)
                                             if a != b and "staging" in (a, b)])
def test_managed_records_reject_other_environment_even_with_seed(credential, stored, requested, tmp_path):
    tmp_path.chmod(0o700)
    source(credential, stored, managed_directory=tmp_path).save(value(credential), None)
    with pytest.raises(KeyUnavailable):
        source(credential, requested, managed_directory=tmp_path, value=value(credential, "seed")).snapshot()


@pytest.mark.parametrize("credential", CREDENTIALS)
def test_staging_rejects_legacy_shared_layout(credential, tmp_path):
    tmp_path.chmod(0o700)
    with pytest.raises(KeyUnavailable, match="LEGACY_LOCAL_MANAGED_DIRECTORY_REQUIRED"):
        source(credential, managed_directory=tmp_path, layout="legacy-shared")


class FakeSecrets:
    """Only the read API exists; these tests cannot write a remote Secret."""
    def __init__(self, record):
        self.record = record

    def get_secret_value(self, **kwargs):
        assert kwargs["VersionStage"] == "AWSCURRENT"
        return {"SecretString": json.dumps(self.record), "VersionId": self.record["revision"],
                "VersionStages": ["AWSCURRENT"]}


@pytest.mark.parametrize("credential", CREDENTIALS)
@pytest.mark.parametrize("record_environment", ("local", *DEPLOYMENTS))
def test_staging_aws_reader_enforces_typed_record_environment(credential, record_environment):
    record = source(credential, record_environment).record(value(credential), str(uuid.uuid4()))
    current = source(credential, aws_secret_id=
                     "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:synthetic/staging-abcdef",
                     aws_client=FakeSecrets(record))
    if record_environment == "staging":
        assert current.snapshot().value == value(credential)
        assert not current.status()["writable"]
    else:
        with pytest.raises(KeyUnavailable, match="AWS_CREDENTIAL_STORE_UNAVAILABLE"):
            current.snapshot()


@pytest.mark.parametrize("credential", CREDENTIALS)
@pytest.mark.parametrize("field,replacement", [("format", "openrouter-key/1"),
                                               ("purpose", "wrong-purpose"),
                                               ("environment", "stage")])
def test_staging_rejects_invalid_record_type(credential, field, replacement):
    current = source(credential)
    record = current.record(value(credential), str(uuid.uuid4()))
    record[field] = replacement
    with pytest.raises(KeyUnavailable):
        current.parse_record(json.dumps(record))


@pytest.mark.parametrize("kind", ("chat", "profiler"))
@pytest.mark.parametrize("other", ("local", "develop", "production"))
def test_request_key_snapshot_does_not_cross_staging_boundary(kind, other):
    current = source("provider-" + kind, value=value("provider-" + kind))
    other_source = source("provider-" + kind, other, value=value("provider-" + kind, other))
    for active, target in ((current, other_source), (other_source, current)):
        token = ACTIVE_KEY.set(active.snapshot())
        try:
            request = httpx.Request("GET", "https://synthetic.invalid")
            assert ProviderAuth(target).authorize(request).headers["authorization"] == "Bearer " + target.snapshot().value
        finally:
            ACTIVE_KEY.reset(token)


@pytest.mark.parametrize("deployment", DEPLOYMENTS)
@pytest.mark.parametrize("database_environment", DEPLOYMENTS)
def test_deployed_database_must_match_environment(monkeypatch, deployment, database_environment):
    monkeypatch.setenv("AI_ENVIRONMENT", deployment)
    database = "ai_db_" + database_environment
    if deployment == database_environment:
        assert database_identity.validate_database_name(database) == database
    else:
        with pytest.raises(ValueError, match="must match AI_ENVIRONMENT"):
            database_identity.validate_database_name(database)


@pytest.mark.parametrize("role", ("ai_chat_service", "ai_profiler_service", "ai_search_service",
                                  "ai_migrator", "ai_catalog_sync"))
def test_staging_dsn_preserves_host_database_and_role_boundaries(monkeypatch, role):
    monkeypatch.setenv("AI_ENVIRONMENT", "staging")
    monkeypatch.setenv("AI_DATABASE_HOST", "staging-db.example.invalid")
    database = "ai_db_staging"
    dsn = f"postgresql://{role}@staging-db.example.invalid:5432/{database}?sslmode=verify-full"
    assert database_identity.validate_dsn(dsn, database, role) == dsn
    for invalid in (dsn.replace("staging-db", "production-db"), dsn.replace(database, "ai_db_production"),
                    dsn.replace(role + "@", "wrong_role@"), dsn.replace(":5432/", ":5433/"),
                    dsn + "&dbname=ai_db_production", dsn + "&host=production-db.example.invalid"):
        with pytest.raises(ValueError):
            database_identity.validate_dsn(invalid, database, role)
    with pytest.raises(ValueError, match="must match AI_ENVIRONMENT"):
        database_identity.validate_dsn(dsn.replace(database, "ai_db_production"), "ai_db_production", role)


@pytest.mark.parametrize("role", ("chat", "profiler", "search"))
def test_launcher_rejects_wrong_environment_before_database_audit(monkeypatch, role):
    monkeypatch.setenv("AI_ENVIRONMENT", "staging")
    monkeypatch.setenv("AI_DATABASE_NAME", "ai_db_production")
    def unexpected(*args):
        pytest.fail("Wrong environment must be rejected before a DB connection")
    monkeypatch.setattr(database_identity, "audit", unexpected)
    with pytest.raises(ValueError, match="must match AI_ENVIRONMENT"):
        entrypoint.database(f"postgresql://ai_{role}_service@postgres/ai_db_production", role)


def test_migration_rejects_wrong_environment_before_creating_engine(monkeypatch):
    from integration import migration_upgrade
    monkeypatch.setenv("AI_ENVIRONMENT", "staging")
    def unexpected(*args, **kwargs):
        pytest.fail("Wrong environment must be rejected before a DB connection")
    monkeypatch.setattr(migration_upgrade.sa, "create_engine", unexpected)
    with pytest.raises(ValueError, match="must match AI_ENVIRONMENT"):
        migration_upgrade.run_upgrade("postgresql://ai_migrator@postgres/ai_db_production", "ai_db_production")


def test_local_database_compatibility_and_synthetic_flag_cannot_bypass_deployed_identity(monkeypatch):
    for deployment in DEPLOYMENTS:
        assert database_identity.validate_database_name("ai_db_" + deployment) == "ai_db_" + deployment
    monkeypatch.setenv("INTEGRATION_SYNTHETIC_ONLY", "1")
    assert database_identity.validate_database_name("emmet4930_fixture") == "emmet4930_fixture"
    monkeypatch.setenv("AI_ENVIRONMENT", "staging")
    for database in ("emmet4930_fixture", "ai_db_develop", "ai_db_production"):
        with pytest.raises(ValueError):
            database_identity.validate_database_name(database)
    monkeypatch.setenv("AI_ENVIRONMENT", "stage")
    with pytest.raises(KeyUnavailable, match="CREDENTIAL_ENVIRONMENT_INVALID"):
        database_identity.validate_database_name("ai_db_staging")


def test_compose_one_off_jobs_receive_same_environment_and_database_host():
    import yaml
    root = Path(__file__).resolve().parents[1] / "containers"
    main = yaml.safe_load((root / "compose.yaml").read_text())["services"]
    sync = yaml.safe_load((root / "compose.sync.yaml").read_text())["services"]["catalog-sync"]
    for job in (main["migrate"], sync):
        for name in ("AI_ENVIRONMENT", "AI_DATABASE_HOST"):
            assert job["environment"][name] == main["search"]["environment"][name]


@pytest.mark.parametrize("role", ("chat", "profiler", "search"))
def test_staging_launcher_keeps_distinct_credentials_and_database(monkeypatch, role):
    from integration.service_token import matches
    env = {"AI_ENVIRONMENT": "staging", "AI_DATABASE_NAME": "ai_db_staging",
           "AI_INTERNAL_SERVICE_TOKEN": "synthetic_internal_token_123456789",
           "BE_SERVICE_TOKEN": "synthetic_shared_be_token_123456789",
           "OPENROUTER_API_KEY_CHAT": value("provider-chat"),
           "OPENROUTER_API_KEY_PROFILER": value("provider-profiler"),
           "SESSION_SECRET": "synthetic_session", "QA_ADMIN_PASSWORD": "synthetic_password",
           "DEMO_ACCESS_TOKEN": "synthetic_demo"}
    for kind, variable in (("chat", "DATABASE_URL"), ("profiler", "PROFILING_DATABASE_URL"),
                           ("search", "SEARCH_CATALOG_DATABASE_URL")):
        env[variable] = f"postgresql://ai_{kind}_service@postgres/ai_db_staging"
    for name, setting in env.items():
        monkeypatch.setenv(name, setting)
    audited = []
    monkeypatch.setattr(database_identity, "audit", lambda dsn, database: audited.append(database))
    monkeypatch.setattr(entrypoint, "profile_database", lambda dsn: None)
    monkeypatch.setattr(entrypoint, "prepare_encoder", lambda *args: None)
    entrypoint.configure(role)
    assert audited == ["ai_db_staging"] and os.environ["AI_ENVIRONMENT"] == "staging"
    internal = {"chat": "SEARCH_TOKEN", "profiler": "PROFILING_SEARCH_TOKEN", "search": "SEARCH_SERVICE_TOKEN"}
    assert os.environ[internal[role]] == env["AI_INTERNAL_SERVICE_TOKEN"]
    if role != "search":
        assert matches(role, "Bearer " + env["BE_SERVICE_TOKEN"])
        assert not matches(role, "Bearer " + env["AI_INTERNAL_SERVICE_TOKEN"])
        assert ProviderKey.from_environment(role).snapshot().value == value("provider-" + role)


def test_staging_sync_rejects_wrong_database_before_export_or_audit(monkeypatch):
    import sys
    from integration import search_sync
    monkeypatch.setenv("AI_ENVIRONMENT", "staging")
    monkeypatch.setenv("AI_DATABASE_NAME", "ai_db_production")
    monkeypatch.setenv("CATALOG_SYNC_DATABASE_URL", "postgresql://ai_catalog_sync@postgres/ai_db_production")
    monkeypatch.setenv("BACKEND_SERVICE_TOKEN", "synthetic_export_token_123456789")
    monkeypatch.setattr(sys, "argv", ["search_sync", "--export-url", "https://synthetic.invalid/products",
                                     "--categories-url", "https://synthetic.invalid/categories"])
    def unexpected(*args):
        pytest.fail("Wrong environment must be rejected before a DB connection or export")
    monkeypatch.setattr(search_sync, "audit", unexpected)
    with pytest.raises(ValueError, match="must match AI_ENVIRONMENT"):
        search_sync.main()


def test_staging_provider_purposes_stay_separate_and_be_token_stays_shared():
    revision = str(uuid.uuid4())
    chat = source("provider-chat")
    record = json.dumps(chat.record(value("provider-chat"), revision))
    for other in (source("provider-profiler"), source("service-chat")):
        with pytest.raises(KeyUnavailable):
            other.parse_record(record)
    shared = source("service-chat").record(value("service-chat"), revision)
    assert source("service-profiler").parse_record(json.dumps(shared)) == (value("service-chat"), revision)
