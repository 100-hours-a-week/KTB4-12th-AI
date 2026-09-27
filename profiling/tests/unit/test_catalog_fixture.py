"""tests/fixtures/catalog_sample.json — 레포 안 예시 카탈로그가 catalog.py로 읽히는지 (파일만 본다. 앱을 띄우는 확인은 tests/integration/test_e2e_app.py).
Catalog 빌드(#9~13)는 이 파일을 7.9 export 응답으로 받아 같은 결과가 나와야 한다."""

from pathlib import Path

from profiling.catalog import (
    FILE_CATALOG_VERSION_ID,
    FileCatalogReader,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "catalog_sample.json"


def test_fixture_loads_with_expected_shape() -> None:
    cat = FileCatalogReader(FIXTURE)
    version_id, products = cat.active()
    assert version_id == FILE_CATALOG_VERSION_ID
    assert len(products) == 111
    assert len({p.categoryName for p in products}) == 56                     # 카테고리 전부
    assert sum(1 for p in products if p.description is None) == 1            # 설명 null 케이스
    assert sum(1 for p in products if p.availability == "unavailable") == 2  # 재고 없음 케이스
    assert [p.productId for p in products] == sorted(p.productId for p in products)   # 7.9: productId 오름차순
    assert sum(1 for p in products if p.viewCount > 0) > 100                    # 조회수(임의 값)가 실려 있음 — v1 정렬 기준
