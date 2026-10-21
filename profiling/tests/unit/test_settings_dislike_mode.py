"""settings — 비선호 처리 스위치 (10-11 결정: v1 exclude · v2 penalty, 제외 규칙은 남김). .env 는 읽지 않는다(_env_file=None)."""

import pytest
from pydantic import ValidationError

from profiling import pipeline
from profiling.settings import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROFILING_DISLIKE_MODE", raising=False)


def test_default_is_exclude() -> None:
    assert Settings(_env_file=None).DISLIKE_MODE == "exclude"


def test_env_name_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFILING_DISLIKE_MODE", "penalty")
    assert Settings(_env_file=None).DISLIKE_MODE == "penalty"


def test_unknown_value_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFILING_DISLIKE_MODE", "soft")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_values_match_pipeline_enum() -> None:
    """설정 값과 pipeline.DislikeMode 값이 같아야 intake 의 DislikeMode(값) 변환이 깨지지 않는다."""
    allowed = set(Settings.model_fields["DISLIKE_MODE"].annotation.__args__)
    assert allowed == {m.value for m in pipeline.DislikeMode}
