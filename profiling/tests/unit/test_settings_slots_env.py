"""settings — 슬롯 수 환경변수 이름 (이슈 2026-09-28_1424).

필드 이름이 PROFILING_SLOTS 라 env_prefix 가 붙으면 PROFILING_PROFILING_SLOTS 가 됐다. 별칭으로 문서 이름
PROFILING_SLOTS 를 읽고, 옛 이름도 계속 받는다. .env 는 읽지 않는다(_env_file=None).
"""

import pytest

from profiling.settings import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROFILING_SLOTS", raising=False)
    monkeypatch.delenv("PROFILING_PROFILING_SLOTS", raising=False)


def test_documented_name_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFILING_SLOTS", "4")
    assert Settings(_env_file=None).PROFILING_SLOTS == 4


def test_old_double_prefix_name_still_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFILING_PROFILING_SLOTS", "3")
    assert Settings(_env_file=None).PROFILING_SLOTS == 3


def test_default_is_one() -> None:
    assert Settings(_env_file=None).PROFILING_SLOTS == 1


def test_other_prefixed_fields_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFILING_POOL_SIZE", "12")
    assert Settings(_env_file=None).POOL_SIZE == 12
