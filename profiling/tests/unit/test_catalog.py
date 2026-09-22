"""catalog.FileCatalogReader — 두 형식 자동 판별·검증·중복 제거가 ports 계약(CatalogReader)대로 도는지. 저장소는 DB뿐이라 tests/integration."""

import json
from pathlib import Path

import pytest

from profiling.catalog import (
    FILE_CATALOG_VERSION_ID,
    FileCatalogReader,
)
from profiling.ports import CatalogReader, NoActiveCatalog

# ---------------------------------------------------------------- 자료

RAW = {  # 동료 공유본 원형 (형식 b) — 실제 파일의 키 그대로
    "schema_version": "test",
    "products": [
        {"product_id": "KAKAO_GIFT:11", "name": "[각인] 립밤", "normalized_name": "립밤", "brand": "B", "description": "설명",
         "category": "메이크업", "category_id": "CAT-01-02", "price_krw": "39000", "sale_status": "ON_SALE", "sold_out": False},
        {"product_id": "KAKAO_GIFT:12", "name": "머그컵", "brand": "C", "description": None,
         "category": "주방용품", "category_id": "CAT-03-01", "price_krw": "12000", "sale_status": "ON_SALE", "sold_out": True},
        {"product_id": "KAKAO_GIFT:11", "name": "중복", "brand": "B", "description": "x",
         "category": "메이크업", "category_id": "CAT-01-02", "price_krw": "1", "sale_status": "ON_SALE", "sold_out": False},
        {"product_id": "KAKAO_GIFT:13", "name": "가격 없음", "brand": "D", "category": "주방용품", "category_id": "CAT-03-01",
         "sale_status": "ON_SALE", "sold_out": False},   # price_krw 없음 → 형식 오류로 버림
    ],
}

EXPORT = {  # 7.9 export 형식 (형식 a)
    "message": "ok",
    "data": {"generatedAt": "2026-09-21T00:00:00Z", "products": [
        {"productId": 21, "name": "A", "brand": "B", "description": None, "categoryId": 102, "categoryName": "메이크업",
         "price": 1000, "available": True, "updatedAt": "2026-09-21T00:00:00Z"},
    ]},
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------- FileCatalogReader


def test_file_catalog_raw_format(tmp_path: Path) -> None:
    cat = FileCatalogReader(_write(tmp_path, RAW))
    assert isinstance(cat, CatalogReader)                       # ports 모양
    version_id, products = cat.active()
    assert version_id == FILE_CATALOG_VERSION_ID
    assert [p.productId for p in products] == [11, 12]         # 중복 11 버림, 가격 없는 13 버림
    lipbalm = cat.by_id(11)
    assert lipbalm is not None
    assert lipbalm.name == "립밤" and lipbalm.categoryId == 102 and lipbalm.categoryName == "메이크업" and lipbalm.price == 39000
    assert lipbalm.available is True
    assert cat.by_id(12).available is False                    # sold_out → 판매 불가
    assert cat.by_id(12).description is None
    assert cat.by_id(999) is None
    assert len(cat) == 2


def test_file_catalog_export_format(tmp_path: Path) -> None:
    cat = FileCatalogReader(_write(tmp_path, EXPORT))
    _, products = cat.active()
    assert len(products) == 1 and products[0].productId == 21


def test_file_catalog_missing_or_empty(tmp_path: Path) -> None:
    with pytest.raises(NoActiveCatalog):
        FileCatalogReader(tmp_path / "없음.json")
    with pytest.raises(NoActiveCatalog):
        FileCatalogReader(_write(tmp_path, {"data": {"products": []}}))
    with pytest.raises(NoActiveCatalog):
        FileCatalogReader(_write(tmp_path, {"hello": 1}))
