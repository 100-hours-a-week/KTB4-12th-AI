"""settings.tagger_model — 추출기별 기본 모델(10-16). TAGGER_MODEL 을 비우면 추출기에 맞는 모델을 쓴다. .env 는 읽지 않는다(_env_file=None)."""

import pytest

from profiling.settings import DEFAULT_TAGGER_MODEL, Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PROFILING_TAGGER", "PROFILING_TAGGER_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(("tagger", "want"), [
    ("openrouter", "google/gemini-3.5-flash-lite"),     # v2 운영 모델(10-14 확정)
    ("ollama", "gemma4:12b"),                           # 로컬 LLM(v3)
    ("none", None),                                     # 꺼져 있으면 모델 없음
])
def test_default_model_follows_tagger(tagger: str, want: str | None) -> None:
    assert Settings(_env_file=None, TAGGER=tagger).tagger_model == want


def test_explicit_model_wins() -> None:
    assert Settings(_env_file=None, TAGGER="openrouter", TAGGER_MODEL="anthropic/x").tagger_model == "anthropic/x"
    assert Settings(_env_file=None, TAGGER="ollama", TAGGER_MODEL="qwen3.5:9b").tagger_model == "qwen3.5:9b"


def test_empty_env_value_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFILING_TAGGER", "openrouter")
    monkeypatch.setenv("PROFILING_TAGGER_MODEL", "")               # .env 에 이름만 있고 값이 빈 경우
    assert Settings(_env_file=None).tagger_model == DEFAULT_TAGGER_MODEL["openrouter"]


def test_every_tagger_has_a_default() -> None:
    tagger_choices = Settings.model_fields["TAGGER"].annotation.__args__
    assert set(DEFAULT_TAGGER_MODEL) == set(tagger_choices) - {"none"}
