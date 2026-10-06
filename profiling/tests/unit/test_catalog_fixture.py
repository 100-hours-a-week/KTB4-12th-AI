"""tests/fixtures/catalog_sample.json — 레포 안 예시 카탈로그가 catalog.py로 읽히는지 (파일만 본다. 앱을 띄우는 확인은 tests/integration/test_e2e_app.py).
Catalog 빌드(#9~13)는 이 파일을 7.9 export 응답으로 받아 같은 결과가 나와야 한다."""

from pathlib import Path

from profiling.catalog import (
    FILE_CATALOG_VERSION_ID,
    FileCatalogReader,
)
from tools.fake_backend import app as fake

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "catalog_sample.json"


def test_fixture_loads_with_expected_shape(monkeypatch) -> None:
    cat = FileCatalogReader(FIXTURE)
    version_id, products = cat.active()
    assert version_id == FILE_CATALOG_VERSION_ID
    assert len(products) == 111
    assert len({p.categoryName for p in products}) == 56                     # 카테고리 전부
    assert sum(1 for p in products if p.description is None) == 1            # 설명 null 케이스
    assert sum(1 for p in products if p.availability == "unavailable") == 2  # 재고 없음 케이스
    assert [p.productId for p in products] == sorted(p.productId for p in products)   # 7.9: productId 오름차순
    assert sum(1 for p in products if p.viewCount > 0) > 100                    # 조회수(임의 값)가 실려 있음 — v1 정렬 기준

    # 가짜 Backend 콘솔이 같은 파일로 화면용 목록을 만든다. 재고가 3값(availability)이 된 뒤에도 `available` 불리언으로 내려줘야 한다
    # (09-30 이슈: 옛 필드 이름을 읽다 KeyError → 콘솔이 카테고리 목록 없이 멈췄다). 재고 없음 2건만 판매 불가, unknown 은 가능.
    monkeypatch.setattr(fake, "CATALOG_FILE", FIXTURE)
    body = fake.console_catalog()
    assert body["count"] == 111 and len(body["categories"]) == 56
    assert all(isinstance(q["available"], bool) for q in body["products"])
    assert sum(1 for q in body["products"] if not q["available"]) == 2

