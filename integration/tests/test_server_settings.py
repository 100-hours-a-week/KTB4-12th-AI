"""Server (ECS) settings — unset keeps the local guarded behaviour unchanged."""

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


def test_budget_mode_defaults_to_guarded_and_rejects_unknown(monkeypatch):
    monkeypatch.delenv("AI_BUDGET_MODE", raising=False)
    assert entrypoint.budget_mode() == "guarded"
    monkeypatch.setenv("AI_BUDGET_MODE", "off")
    with pytest.raises(ValueError):
        entrypoint.budget_mode()
    monkeypatch.setenv("AI_BUDGET_MODE", "provider-key-limits")
    assert entrypoint.budget_mode() == "provider-key-limits"
    with pytest.raises(ValueError):
        entrypoint.budget_mode(smoke=True)


def test_guarded_fence_only_approves_the_pinned_model(monkeypatch):
    monkeypatch.delenv("PROFILING_TAGGER_MODEL", raising=False)
    assert entrypoint.model_for("PROFILING_TAGGER_MODEL", "guarded") == "openai/gpt-6-luna"
    monkeypatch.setenv("PROFILING_TAGGER_MODEL", "google/gemini-3.5-flash-lite")
    with pytest.raises(ValueError):
        entrypoint.model_for("PROFILING_TAGGER_MODEL", "guarded")
    assert (entrypoint.model_for("PROFILING_TAGGER_MODEL", "provider-key-limits")
            == "google/gemini-3.5-flash-lite")


def test_missing_ledger_without_explicit_mode_still_blocks(launcher):
    with pytest.raises((KeyError, ValueError)):
        entrypoint.configure("profiler")


def test_provider_mode_profiler_uses_configured_model_without_ledger(launcher):
    launcher.setenv("AI_BUDGET_MODE", "provider-key-limits")
    launcher.setenv("PROFILING_TAGGER_MODEL", "google/gemini-3.5-flash-lite")
    assert entrypoint.configure("profiler") is None
    env = entrypoint.os.environ
    assert env["PROFILING_TAGGER_MODEL"] == "google/gemini-3.5-flash-lite"
    assert env["PROFILING_OPENROUTER_API_KEY"] == "unit-openrouter_api_key"
    assert env["PROFILING_TAG_SPEC"] == "tag3disliked"
    assert "PROFILING_BUDGET_URL" not in env and "STORE_TOKEN" not in env


def test_provider_mode_chat_uses_configured_model_without_ledger(launcher):
    launcher.setenv("AI_BUDGET_MODE", "provider-key-limits")
    launcher.setenv("CHAT_MODEL", "openai/gpt-6-luna")
    assert entrypoint.configure("chat") is None
    env = entrypoint.os.environ
    assert env["CHAT_MODEL"] == "openai/gpt-6-luna" and env["CHAT_RUNTIME_MODE"] == "live"
    assert "BUDGET_STORE_URL" not in env


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
