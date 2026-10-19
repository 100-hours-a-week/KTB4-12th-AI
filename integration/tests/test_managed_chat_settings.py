"""Synthetic settings only: no provider, server, model download, or existing DB."""
import os
from pathlib import Path
import secrets

import pytest

from integration.containers import entrypoint
from integration.provider_key import KeyUnavailable
from integration.service_token import ServiceToken, matches


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("AI_", "OPENROUTER_", "BE_", "PROFILING_", "CHAT_", "BUDGET_", "CONSOLE_")):
            monkeypatch.delenv(name, raising=False)
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


def test_common_token_rotation_applies_to_both_services(tmp_path, monkeypatch):
    monkeypatch.setenv("BE_SERVICE_TOKEN_DIR", str(tmp_path))
    source = ServiceToken.from_environment("chat")
    first, second = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    saved = source.save(first, None)
    assert source.status()["appliesTo"] == ["chat", "profiler"]
    assert matches("chat", "Bearer " + first)
    assert matches("profiler", "Bearer " + first)
    source.save(second, saved["revision"])
    assert not matches("chat", "Bearer " + first)
    assert matches("chat", "Bearer " + second)
    assert not matches("profiler", "Bearer " + first)
    assert matches("profiler", "Bearer " + second)


def test_chat_managed_mode_rejects_its_external_token(tmp_path):
    with pytest.raises(KeyUnavailable, match="SOURCE_CONFLICT"):
        ServiceToken.from_environment("chat", {"BE_SERVICE_TOKEN_DIR": str(tmp_path),
                                               "AI_SERVICE_TOKEN": "synthetic-conflict"})


def test_managed_chat_bootstraps_without_sending_or_requiring_saved_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("BE_SERVICE_TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_MANAGED_KEY_DIR", str(tmp_path))
    monkeypatch.setenv("AI_BUDGET_MODE", "provider-key-limits")
    monkeypatch.setenv("CHAT_MODEL", "synthetic/chat-model")
    monkeypatch.setattr(entrypoint, "secret", lambda name, **kw: "synthetic-" + name.lower())
    monkeypatch.setattr(entrypoint, "database", lambda value, role: value)
    assert entrypoint.configure("chat") is None
    assert os.environ["AI_INTERNAL_SERVICE_TOKEN"] == "synthetic-ai_internal_service_token"
    assert "AI_SERVICE_TOKEN" not in os.environ and "OPENROUTER_API_KEY" not in os.environ
    assert os.environ["SEARCH_TOKEN"] == "synthetic-ai_internal_service_token"
    assert os.environ["CHAT_MODEL"] == "synthetic/chat-model"
    assert not list(tmp_path.iterdir())


def test_managed_profiler_bootstraps_without_saved_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("BE_SERVICE_TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_MANAGED_KEY_DIR", str(tmp_path))
    monkeypatch.setenv("AI_BUDGET_MODE", "provider-key-limits")
    monkeypatch.setenv("PROFILING_TAGGER_MODEL", "synthetic/profiler-model")
    monkeypatch.setattr(entrypoint, "secret", lambda name, **kw: "synthetic-" + name.lower())
    monkeypatch.setattr(entrypoint, "database", lambda value, role: value)
    monkeypatch.setattr(entrypoint, "profile_database", lambda value: None)
    assert entrypoint.configure("profiler") is None
    assert os.environ["PROFILING_SERVICE_TOKEN"] == ""
    assert os.environ["PROFILING_SEARCH_TOKEN"] == "synthetic-ai_internal_service_token"
    assert "PROFILING_OPENROUTER_API_KEY" not in os.environ
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("role", ["chat", "profiler"])
@pytest.mark.parametrize("dashboard", [False, True])
def test_team_secret_injection_boots_with_separate_model_keys_and_common_be_token(tmp_path, monkeypatch, role, dashboard):
    from integration.provider_key import ProviderKey
    env = {"AI_ENVIRONMENT": "develop", "AI_BUDGET_MODE": "provider-key-limits",
           "OPENROUTER_API_KEY": "sk-or-v1-" + "synthetic_chat_" * 4,
           "PROFILING_OPENROUTER_API_KEY": "sk-or-v1-" + "synthetic_profiler_" * 4,
           "BE_SERVICE_TOKEN": "synthetic_common_BE_" * 3,
           "AI_INTERNAL_SERVICE_TOKEN": "synthetic_internal_" * 3,
           "DATABASE_URL": "synthetic-chat-db", "PROFILING_DATABASE_URL": "synthetic-profiler-db",
           "SESSION_SECRET": "synthetic-session", "QA_ADMIN_PASSWORD": "synthetic-admin",
           "DEMO_ACCESS_TOKEN": "synthetic-demo"}
    if dashboard:
        env.update(OPENROUTER_MANAGED_KEY_DIR=str(tmp_path), BE_SERVICE_TOKEN_DIR=str(tmp_path))
    for name, value in env.items(): monkeypatch.setenv(name, value)
    monkeypatch.setattr(entrypoint, "database", lambda value, role: value)
    monkeypatch.setattr(entrypoint, "profile_database", lambda value: None)
    assert entrypoint.configure(role) is None
    variable = "OPENROUTER_API_KEY" if role == "chat" else "PROFILING_OPENROUTER_API_KEY"
    assert ProviderKey.from_environment(role).snapshot().value == env[variable]
    assert ServiceToken.from_environment(role).snapshot().value == env["BE_SERVICE_TOKEN"]
    assert os.environ["SEARCH_TOKEN" if role == "chat" else "PROFILING_SEARCH_TOKEN"] == env["AI_INTERNAL_SERVICE_TOKEN"]
    assert os.environ["CHAT_MODEL" if role == "chat" else "PROFILING_TAGGER_MODEL"] == entrypoint.PINNED_MODEL
    assert not list(tmp_path.iterdir())


def test_explicit_console_config_starts_without_dependency_probe(tmp_path, monkeypatch):
    config = tmp_path / "targets.json"
    config.write_text('{"targets":{}}')
    monkeypatch.setenv("CONSOLE_TARGET_FILE", str(config))
    monkeypatch.setattr(entrypoint, "secret", lambda *a, **kw: pytest.fail("No dependency probe"))
    assert entrypoint.configure("console") is None
    assert config.read_text() == '{"targets":{}}'


@pytest.mark.parametrize("invalid", ["*", "127.0.0.0/8", "proxy.local"])
def test_console_proxy_requires_exact_ip_addresses(monkeypatch, invalid):
    monkeypatch.setenv("CONSOLE_TRUSTED_PROXY_IPS", invalid)
    with pytest.raises(ValueError):
        entrypoint.console_proxy_ips()


def test_console_runtime_dependencies_are_in_build_context():
    root = Path(__file__).resolve().parents[2]
    docker = (root / "integration/containers/Dockerfile").read_text()
    ignore = (root / "integration/containers/Dockerfile.dockerignore").read_text().splitlines()
    for name in ("admin_auth.py", "products.py"):
        assert "console/" + name in docker and "!console/" + name in ignore
    assert "model-assets" not in docker
    compose = (root / "integration/containers/compose.smoke.yaml").read_text()
    assert "EMMET_MODEL_DIR" in compose and "read_only: true" in compose


def test_chat_health_uses_external_be_token_instead_of_internal_token(tmp_path, monkeypatch):
    import io
    import sys
    from types import SimpleNamespace
    from integration.containers import health
    path = tmp_path / "external-chat-token"
    path.write_text("synthetic-distinct-chat-token")
    monkeypatch.setenv("CHAT_BE_SERVICE_TOKEN_FILE", str(path))
    monkeypatch.setenv("AI_SERVICE_TOKEN", "synthetic-internal-token")
    monkeypatch.setattr(sys, "argv", ["health", "chat"])
    seen = []
    def send(request, **kwargs):
        seen.append(request.get_header("Authorization"))
        return io.BytesIO(b'{"ready":true}')
    monkeypatch.setattr(health.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=send))
    health.main()
    assert seen == ["Bearer synthetic-distinct-chat-token"]
