"""통합 테스트 공통."""

from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


@pytest.fixture(scope="session")
def alembic_head() -> str:
    """alembic/versions/ 의 가장 높은 리비전. 마이그레이션이 늘 때마다 시험을 고치지 않으려고 파일에서 읽는다."""
    return max(p.name.split("_", 1)[0] for p in VERSIONS.glob("[0-9]*.py"))


def pytest_collection_modifyitems(config, items) -> None:
    """DB 주소(PROFILING_DATABASE_URL)가 없으면 통합 시험 전체를 건너뛴다 — 기본값이 없어(공개 계정 금지) 연결 오류 대신 skip."""
    from profiling.settings import get_settings

    if get_settings().DATABASE_URL:
        return
    skip = pytest.mark.skip(reason="PROFILING_DATABASE_URL 없음 — DB 통합 시험 건너뜀")
    here = Path(__file__).resolve().parent
    for item in items:
        if here in Path(item.path).resolve().parents:
            item.add_marker(skip)
