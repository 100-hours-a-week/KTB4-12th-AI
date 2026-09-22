"""tests/fixtures/catalog_sample.json — 레포 안 예시 카탈로그가 adapter로 읽히고 앱이 그걸로 뜨는지.
DB adapter(#5~7)·Catalog 빌드(#9~13)는 이 파일을 7.9 export 응답으로 받아 같은 결과가 나와야 한다."""

from pathlib import Path

from fastapi.testclient import TestClient

from profiling.catalog import (
    FILE_CATALOG_VERSION_ID,
    FileCatalogReader,
)
from profiling.main import app

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "catalog_sample.json"


def test_fixture_loads_with_expected_shape() -> None:
    cat = FileCatalogReader(FIXTURE)
    version_id, products = cat.active()
    assert version_id == FILE_CATALOG_VERSION_ID
    assert len(products) == 111
    assert len({p.categoryName for p in products}) == 56                     # 카테고리 전부
    assert sum(1 for p in products if p.description is None) == 1            # 설명 null 케이스
    assert sum(1 for p in products if not p.available) == 2                  # 판매 불가 케이스
    assert [p.productId for p in products] == sorted(p.productId for p in products)   # 7.9: productId 오름차순
    assert sum(1 for p in products if p.viewCount > 0) > 100                    # 조회수(임의 값)가 실려 있음 — v1 정렬 기준


def test_app_health_with_fixture(monkeypatch) -> None:
    monkeypatch.setenv("PROFILING_CATALOG_FILE", str(FIXTURE))
    from profiling.settings import get_settings
    get_settings.cache_clear()
    with TestClient(app) as client:                                          # lifespan 실행 → FileCatalogReader 로드
        res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["catalog"] == {"active": True, "version": str(FILE_CATALOG_VERSION_ID), "products": 111}
    get_settings.cache_clear()
