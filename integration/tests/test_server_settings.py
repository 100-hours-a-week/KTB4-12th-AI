"""Server settings use provider limits by default; ledger runs opt into guarded."""

import os

import pytest

from integration import database_identity
from integration.containers import entrypoint

DB = "emmet4930_test"


def dsn(host, role="ai_profiler_service", db=DB):
    return f"postgresql+psycopg://{role}@{host}:5432/{db}"


# 1. DB host -----------------------------------------------------------------

def test_host_defaults_to_local_postgres(monkeypatch):
    monkeypatch.delenv("AI_DATABASE_HOST", raising=False)
    assert database_identity.configured_host() == "postgres"
    database_identity.validate_dsn(dsn("postgres"), DB, "ai_profiler_service")
    with pytest.raises(ValueError):
        database_identity.validate_dsn(dsn("db.internal.example"), DB, "ai_profiler_service")


def test_configured_host_is_the_only_accepted_host(monkeypatch):
    monkeypatch.setenv("AI_DATABASE_HOST", "ai-db.abc123.ap-northeast-2.rds.amazonaws.com")
    database_identity.validate_dsn(
        dsn("ai-db.abc123.ap-northeast-2.rds.amazonaws.com"), DB, "ai_profiler_service"
    )
    with pytest.raises(ValueError):
        database_identity.validate_dsn(dsn("postgres"), DB, "ai_profiler_service")


@pytest.mark.parametrize("bad", ["http://db", "db:5432", "db/x", "-db", "db name"])
def test_host_setting_must_be_a_bare_hostname(monkeypatch, bad):
    monkeypatch.setenv("AI_DATABASE_HOST", bad)
    with pytest.raises(ValueError):
        database_identity.configured_host()


def test_app_launcher_uses_the_configured_host(monkeypatch):
    host = "ai-db.abc123.ap-northeast-2.rds.amazonaws.com"
    monkeypatch.setenv("AI_DATABASE_HOST", host)
    monkeypatch.setenv("AI_DATABASE_NAME", DB)
    monkeypatch.setattr(database_identity, "audit", lambda *a: None)
    assert entrypoint.database(dsn(host), "profiler") == dsn(host)
    with pytest.raises(ValueError):
        entrypoint.database(dsn("postgres"), "profiler")


# 2. Secrets: file first, else the same-named variable ------------------------

def test_secret_file_wins_over_variable(tmp_path, monkeypatch):
    f = tmp_path / "token"
    f.write_text("from-file\n")
    monkeypatch.setenv("AI_SERVICE_TOKEN_FILE", str(f))
    monkeypatch.setenv("AI_SERVICE_TOKEN", "from-variable")
    assert entrypoint.secret("AI_SERVICE_TOKEN") == "from-file"


def test_secret_falls_back_to_injected_variable(monkeypatch):
    monkeypatch.delenv("AI_SERVICE_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AI_SERVICE_TOKEN", " from-secrets-manager ")
    assert entrypoint.secret("AI_SERVICE_TOKEN") == "from-secrets-manager"


def test_missing_or_empty_secret_still_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_SERVICE_TOKEN_FILE", raising=False)
    monkeypatch.delenv("AI_SERVICE_TOKEN", raising=False)
    with pytest.raises(ValueError):
        entrypoint.secret("AI_SERVICE_TOKEN")
    assert entrypoint.secret("AI_SERVICE_TOKEN", required=False) is None
    monkeypatch.setenv("AI_SERVICE_TOKEN", "   ")
    with pytest.raises(ValueError):
        entrypoint.secret("AI_SERVICE_TOKEN")
    empty = tmp_path / "empty"
    empty.write_text("\n")
    monkeypatch.setenv("AI_SERVICE_TOKEN_FILE", str(empty))
    monkeypatch.setenv("AI_SERVICE_TOKEN", "variable-does-not-rescue-an-empty-file")
    with pytest.raises(ValueError):
        entrypoint.secret("AI_SERVICE_TOKEN")


def test_health_check_reads_token_the_same_way(tmp_path, monkeypatch):
    from integration.containers import health

    monkeypatch.delenv("AI_SERVICE_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AI_SERVICE_TOKEN", "from-variable")
    assert health.service_token() == "from-variable"
    f = tmp_path / "token"
    f.write_text("from-file")
    monkeypatch.setenv("AI_SERVICE_TOKEN_FILE", str(f))
    assert health.service_token() == "from-file"
    monkeypatch.delenv("AI_SERVICE_TOKEN_FILE")
    monkeypatch.delenv("AI_SERVICE_TOKEN")
    with pytest.raises(SystemExit):
        health.service_token()


# 3 · 4. Cost guard mode and models -------------------------------------------

@pytest.fixture
def launcher(monkeypatch):
    """configure() with secrets/DB/audit stubbed; no network, no files."""
    for name in ["AI_BUDGET_MODE", "PROFILING_TAGGER_MODEL", "CHAT_MODEL",
                 "PROFILING_BUDGET_URL", "BUDGET_STORE_URL", "STORE_TOKEN",
                 "BUDGET_GUARDED_ORIGIN", "BUDGET_EXPECTED_MAX_ATTEMPTS",
                 "BUDGET_EXPECTED_MAX_RESERVED_USD"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(entrypoint, "secret", lambda name, **kw: "unit-" + name.lower())
    monkeypatch.setattr(entrypoint, "database", lambda value, role: value)
    monkeypatch.setattr(entrypoint, "profile_database", lambda value: None)
    saved = dict(os.environ)
    yield monkeypatch
    # configure() writes os.environ directly; put it back for later tests.
    os.environ.clear()
    os.environ.update(saved)


def test_budget_mode_defaults_to_provider_limits_and_requires_explicit_guarded(monkeypatch):
    monkeypatch.delenv("AI_BUDGET_MODE", raising=False)
    for value in (None, "", "  ", "provider-key-limits"):
        if value is not None:
            monkeypatch.setenv("AI_BUDGET_MODE", value)
        assert entrypoint.budget_mode() == "provider-key-limits"
        with pytest.raises(ValueError):
            entrypoint.budget_mode(smoke=True)
    monkeypatch.setenv("AI_BUDGET_MODE", "guarded")
    assert entrypoint.budget_mode() == entrypoint.budget_mode(smoke=True) == "guarded"
    for value in ("off", "GUARDED", "auto"):
        monkeypatch.setenv("AI_BUDGET_MODE", value)
        with pytest.raises(ValueError):
            entrypoint.budget_mode()


def test_guarded_fence_only_approves_the_pinned_model(monkeypatch):
    monkeypatch.delenv("PROFILING_TAGGER_MODEL", raising=False)
    assert entrypoint.model_for("PROFILING_TAGGER_MODEL", "guarded") == "openai/gpt-6-luna"
    monkeypatch.setenv("PROFILING_TAGGER_MODEL", "google/gemini-3.5-flash-lite")
    with pytest.raises(ValueError):
        entrypoint.model_for("PROFILING_TAGGER_MODEL", "guarded")
    assert (entrypoint.model_for("PROFILING_TAGGER_MODEL", "provider-key-limits")
            == "google/gemini-3.5-flash-lite")


@pytest.mark.parametrize("role", ["chat", "profiler"])
def test_explicit_guarded_without_ledger_still_blocks(launcher, role):
    launcher.setenv("AI_BUDGET_MODE", "guarded")
    with pytest.raises((KeyError, ValueError)):
        entrypoint.configure(role)


@pytest.mark.parametrize("mode", [None, "provider-key-limits"])
def test_provider_mode_profiler_uses_configured_model_without_ledger(launcher, mode):
    if mode is not None:
        launcher.setenv("AI_BUDGET_MODE", mode)
    launcher.setenv("PROFILING_TAGGER_MODEL", "google/gemini-3.5-flash-lite")
    assert entrypoint.configure("profiler") is None
    env = entrypoint.os.environ
    assert env["PROFILING_TAGGER_MODEL"] == "google/gemini-3.5-flash-lite"
    assert env["PROFILING_OPENROUTER_API_KEY"] == "unit-profiling_openrouter_api_key"
    assert env["PROFILING_TAG_SPEC"] == "tag3disliked"
    assert "PROFILING_BUDGET_URL" not in env and "STORE_TOKEN" not in env


@pytest.mark.parametrize("mode", [None, "provider-key-limits"])
def test_provider_mode_chat_uses_configured_model_without_ledger(launcher, mode):
    if mode is not None:
        launcher.setenv("AI_BUDGET_MODE", mode)
    launcher.setenv("CHAT_MODEL", "openai/gpt-6-luna")
    assert entrypoint.configure("chat") is None
    env = entrypoint.os.environ
    assert env["CHAT_MODEL"] == "openai/gpt-6-luna" and env["CHAT_RUNTIME_MODE"] == "live"
    assert "BUDGET_STORE_URL" not in env


@pytest.mark.parametrize("mode", [None, "", "  ", "provider-key-limits", "guarded", "off"])
def test_native_profiler_selects_policy_before_database_or_model(monkeypatch, mode):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from integration import profiler_runtime as runtime

    monkeypatch.delenv("AI_BUDGET_MODE", raising=False)
    if mode is not None:
        monkeypatch.setenv("AI_BUDGET_MODE", mode)
    calls = []
    monkeypatch.setattr(runtime, "get_settings", lambda: SimpleNamespace(
        SERVICE_TOKEN="synthetic", TAGGER="openrouter", CATALOG_SOURCE="db", EMBEDDING_DIR="/synthetic"))
    monkeypatch.setattr(runtime, "ProviderKey", SimpleNamespace(from_environment=lambda *args: object()))
    monkeypatch.setattr(runtime, "shared_token_configured", lambda: False)
    monkeypatch.setattr(runtime.main, "strict_problems", lambda *a, **kw: [])

    class Budget:
        def __init__(self, *args): calls.append("ledger")
        def call(self, method): calls.append(method)
        def close(self): calls.append("close")

    monkeypatch.setattr(runtime, "SharedBudget", Budget)
    monkeypatch.setattr(runtime, "BudgetedExtractor", lambda *a, **kw: calls.append("guarded"))
    monkeypatch.setattr(runtime, "ProviderLimitedExtractor", lambda *a, **kw: calls.append("provider-key-limits"))
    original = runtime.llm.OpenRouterTagExtractor

    class BeforeDatabase(Exception): pass

    @asynccontextmanager
    async def database_boundary(app):
        runtime.llm.OpenRouterTagExtractor()
        raise BeforeDatabase()
        yield

    monkeypatch.setattr(runtime.main, "lifespan", database_boundary)
    async def case():
        with pytest.raises(RuntimeError if mode == "off" else BeforeDatabase):
            async with runtime.lifespan(SimpleNamespace(state=SimpleNamespace())):
                pytest.fail("Synthetic check must stop before the DB boundary")
    asyncio.run(case())
    assert calls == ([] if mode == "off" else ["ledger", "budget_summary", "guarded", "close"]
                     if mode == "guarded" else ["provider-key-limits"])
    assert runtime.llm.OpenRouterTagExtractor is original


def test_provider_limited_extractor_keeps_destination_and_generation_checks():
    import httpx
    from integration.profiler_boundary import ProviderLimitedExtractor, PreflightTransport

    with pytest.raises(ValueError):
        ProviderLimitedExtractor("https://evil.example/api/v1", "k", "google/gemini-3.5-flash-lite")
    with pytest.raises(ValueError):
        ProviderLimitedExtractor("https://openrouter.ai/api/v1", "k", "")
    sent = []
    upstream = httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200, json={"ok": True}))
    guard = PreflightTransport(upstream)
    ok = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions", json={})
    assert guard.handle_request(ok).status_code == 200 and len(sent) == 1
    with pytest.raises(httpx.TransportError):
        guard.handle_request(httpx.Request("POST", "https://evil.example/api/v1/chat/completions"))
    guard.preflight = lambda: (_ for _ in ()).throw(RuntimeError("generation changed"))
    with pytest.raises(httpx.TransportError):
        guard.handle_request(ok)
    assert len(sent) == 1
    failing = PreflightTransport(httpx.MockTransport(lambda r: httpx.Response(500, text="provider secret detail")))
    redacted = failing.handle_request(ok)
    assert redacted.status_code == 500 and "secret detail" not in redacted.text


def test_provider_mode_counts_as_a_required_model_guard():
    from integration.profiler_health import model_state

    health = {"llm": {"enabled": True, "model": "google/gemini-3.5-flash-lite", "state": "ok"},
              "integration": {"modelRequired": True, "budgetGuard": "provider-key-limits"}}
    assert model_state(health)["required"] is True
    health["integration"]["budgetGuard"] = "unverified"
    assert model_state(health)["required"] is False


@pytest.mark.parametrize("source_kind", ["dotenv", "environment", "file", "missing_file", "managed", "saved_missing", "aws", "legacy"])
def test_native_profiler_dotenv_key_preserves_explicit_and_managed_sources(tmp_path, monkeypatch, source_kind):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from profiling.settings import Settings
    from integration import profiler_runtime as runtime
    from integration.provider_key import ProviderKey, KeyUnavailable

    for name in tuple(os.environ):
        if any(part in name for part in ("OPENROUTER", "CREDENTIAL", "BE_SERVICE_TOKEN", "AI_BUDGET_MODE")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AI_ENVIRONMENT", "local")
    dotenv = tmp_path / "synthetic.env"
    dotenv.write_text("PROFILING_OPENROUTER_API_KEY=synthetic-dotenv-key\n")
    settings = Settings(_env_file=dotenv, SERVICE_TOKEN="synthetic", TAGGER="openrouter",
                        CATALOG_SOURCE="db", EMBEDDING_DIR="/synthetic")
    assert settings.OPENROUTER_API_KEY.get_secret_value() == "synthetic-dotenv-key"
    expected = "synthetic-dotenv-key"
    if source_kind == "environment":
        monkeypatch.setenv("PROFILING_OPENROUTER_API_KEY", "synthetic-environment-key")
        expected = "synthetic-environment-key"
    elif source_kind in {"file", "missing_file"}:
        key_file = tmp_path / "synthetic-key"
        if source_kind == "file": key_file.write_text("synthetic-file-key")
        monkeypatch.setenv("PROFILING_OPENROUTER_API_KEY_FILE", str(key_file))
        expected = "synthetic-file-key" if source_kind == "file" else None
    elif source_kind in {"managed", "saved_missing", "legacy"}:
        directory = tmp_path / "managed"; directory.mkdir(mode=0o700)
        monkeypatch.setenv("OPENROUTER_MANAGED_KEY_DIR", str(directory))
        if source_kind == "legacy":
            monkeypatch.setenv("AI_CREDENTIAL_LAYOUT", "legacy-shared")
            expected = None
        else:
            managed = ProviderKey(kind="profiler", managed_directory=str(directory))
            expected = "sk-or-v1-" + "a" * 32
            managed.save(expected, None)
            if source_kind == "saved_missing":
                (directory / managed.record_file).unlink()
                expected = None
    elif source_kind == "aws":
        monkeypatch.setenv("AI_ENVIRONMENT", "develop")
        monkeypatch.setenv("AI_CREDENTIAL_BACKEND", "aws-secrets-manager")
        monkeypatch.setenv("OPENROUTER_PROFILER_SECRET_ARN", "synthetic-typed-secret")
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime, "shared_token_configured", lambda: False)
    monkeypatch.setattr(runtime.main, "strict_problems", lambda *a, **kw: [])

    class BeforeDatabase(Exception): pass
    @asynccontextmanager
    async def before_database(app):
        source = app.state.provider_key
        if source_kind == "aws":
            assert source.mode == "aws" and source._value is None
        elif expected is None:
            with pytest.raises(KeyUnavailable): source.snapshot()
        else:
            assert source.snapshot().value == expected
        raise BeforeDatabase()
        yield
    monkeypatch.setattr(runtime.main, "lifespan", before_database)
    async def case():
        with pytest.raises(BeforeDatabase):
            async with runtime.lifespan(SimpleNamespace(state=SimpleNamespace())): pass
    asyncio.run(case())
