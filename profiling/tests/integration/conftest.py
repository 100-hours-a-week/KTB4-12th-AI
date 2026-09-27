"""통합 테스트 공통."""

from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


@pytest.fixture(scope="session")
def alembic_head() -> str:
    """alembic/versions/ 의 가장 높은 리비전. 마이그레이션이 늘 때마다 시험을 고치지 않으려고 파일에서 읽는다."""
    return max(p.name.split("_", 1)[0] for p in VERSIONS.glob("[0-9]*.py"))
