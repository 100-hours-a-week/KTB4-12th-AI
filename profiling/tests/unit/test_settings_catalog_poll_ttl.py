"""settings — 카탈로그 활성 id 폴링 TTL (09-28 왕복 줄이기). .env 는 읽지 않는다(_env_file=None)."""

import pytest

from profiling.settings import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROFILING_CATALOG_POLL_TTL_S", raising=False)


def test_default_is_one_second() -> None:
    assert Settings(_env_file=None).CATALOG_POLL_TTL_S == 1.0


def test_env_name_is_read_and_zero_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFILING_CATALOG_POLL_TTL_S", "0")
    assert Settings(_env_file=None).CATALOG_POLL_TTL_S == 0.0
