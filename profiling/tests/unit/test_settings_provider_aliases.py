"""Standalone profiler aliases; integration owns files and source conflicts."""

import pytest
from pydantic import SecretStr

from profiling.settings import Settings


@pytest.fixture(autouse=True)
def clean_provider_env(monkeypatch):
    for name in (
        "OPENROUTER_API_KEY_PROFILER",
        "PROFILING_OPENROUTER_API_KEY",
        "OPENROUTER_API_KEY_CHAT",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "name", ["OPENROUTER_API_KEY_PROFILER", "PROFILING_OPENROUTER_API_KEY"]
)
def test_profiler_accepts_current_and_legacy_names(monkeypatch, name):
    monkeypatch.setenv(name, "synthetic-profiler-key")
    settings = Settings(_env_file=None)
    assert settings.OPENROUTER_API_KEY.get_secret_value() == "synthetic-profiler-key"


@pytest.mark.parametrize("name", ["OPENROUTER_API_KEY_CHAT", "OPENROUTER_API_KEY"])
def test_chat_key_never_supplies_native_profiler(monkeypatch, name):
    monkeypatch.setenv(name, "synthetic-chat-key")
    assert Settings(_env_file=None).OPENROUTER_API_KEY is None


def test_existing_field_name_constructor_is_preserved():
    settings = Settings(
        _env_file=None, OPENROUTER_API_KEY=SecretStr("synthetic-constructor-key")
    )
    assert settings.OPENROUTER_API_KEY.get_secret_value() == "synthetic-constructor-key"


@pytest.mark.parametrize(
    "name", ["OPENROUTER_API_KEY_PROFILER", "PROFILING_OPENROUTER_API_KEY"]
)
def test_both_names_are_read_from_dotenv(tmp_path, name):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{name}=synthetic-dotenv-key\n")
    settings = Settings(_env_file=dotenv)
    assert settings.OPENROUTER_API_KEY.get_secret_value() == "synthetic-dotenv-key"


@pytest.mark.parametrize(
    "env_name,dotenv_name",
    [
        ("OPENROUTER_API_KEY_PROFILER", "PROFILING_OPENROUTER_API_KEY"),
        ("PROFILING_OPENROUTER_API_KEY", "OPENROUTER_API_KEY_PROFILER"),
    ],
)
def test_environment_precedes_dotenv_across_aliases(
    monkeypatch, tmp_path, env_name, dotenv_name
):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{dotenv_name}=synthetic-dotenv-key\n")
    monkeypatch.setenv(env_name, "synthetic-environment-key")
    settings = Settings(_env_file=dotenv)
    assert settings.OPENROUTER_API_KEY.get_secret_value() == "synthetic-environment-key"


@pytest.mark.parametrize("source", ["environment", "dotenv"])
@pytest.mark.parametrize("current", ["synthetic-current-key", ""])
def test_native_alias_priority_is_explicit(monkeypatch, tmp_path, source, current):
    """Native settings preserve Pydantic alias priority, including explicit blanks."""
    values = {
        "OPENROUTER_API_KEY_PROFILER": current,
        "PROFILING_OPENROUTER_API_KEY": "synthetic-legacy-key",
    }
    dotenv = None
    if source == "environment":
        for name, value in values.items():
            monkeypatch.setenv(name, value)
    else:
        dotenv = tmp_path / ".env"
        dotenv.write_text("".join(f"{name}={value}\n" for name, value in values.items()))
    assert Settings(_env_file=dotenv).OPENROUTER_API_KEY.get_secret_value() == current


def test_provider_key_is_still_redacted():
    settings = Settings(_env_file=None, OPENROUTER_API_KEY="synthetic-private-value")
    assert "synthetic-private-value" not in repr(settings)
    assert "synthetic-private-value" not in settings.model_dump_json()


def test_other_prefixed_settings_keep_their_names(monkeypatch):
    monkeypatch.setenv("PROFILING_POOL_SIZE", "12")
    monkeypatch.setenv("PROFILING_SLOTS", "4")
    settings = Settings(_env_file=None)
    assert settings.POOL_SIZE == 12
    assert settings.PROFILING_SLOTS == 4
