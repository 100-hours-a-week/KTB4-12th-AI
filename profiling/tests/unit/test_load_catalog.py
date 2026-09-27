"""tools/catalog/load_catalog 의 읽기·검사 — 파일만 본다(DB 없음). 적재 SQL 은 tests/integration/test_db_catalog.py."""

import json

import pytest

from tools.catalog import load_catalog as lc

CATS = [
    {"sourceCategoryId": "GROUP-01", "parentSourceCategoryId": None, "name": "뷰티", "level": 1, "productCount": 2},
    {"sourceCategoryId": "CAT-01-01", "parentSourceCategoryId": "GROUP-01", "name": "스킨케어", "level": 2, "productCount": 2},
]


def _product(sid: str, cat: str = "CAT-01-01") -> dict:
    return {"sourceProductId": sid, "productName": "p", "brandName": "b", "sourceCategoryId": cat,
            "productKind": "k", "productType": "Shipping", "description": "d", "attributes": {},
            "unitPrice": 1000, "listPrice": None, "currency": "KRW", "stockQuantity": None,
            "currentAvailability": None,
            "source": {"provider": "KAKAO_GIFT", "productUrl": "https://x/1", "imageUrl": "https://x/1.jpg"},
            "images": [{"assetId": "a" * 24, "displayOrder": 1}]}


def _write(root, *, cats=None, prods=None, count=None):
    (root / "data").mkdir(parents=True, exist_ok=True)
    prods = prods if prods is not None else [_product("KAKAO_GIFT:1"), _product("KAKAO_GIFT:2")]
    (root / "data/categories.json").write_text(
        json.dumps({"taxonomyVersion": "2026-09-15.final57", "categories": cats if cats is not None else CATS},
                   ensure_ascii=False), encoding="utf-8")
    (root / "data/products.jsonl").write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in prods), encoding="utf-8")
    (root / "data/summary.json").write_text(
        json.dumps({"packageId": "test-pkg", "productCount": count if count is not None else len(prods)}), encoding="utf-8")
    return root


def test_read_package_ok(tmp_path) -> None:
    pkg = lc.read_package(_write(tmp_path))
    assert pkg["taxonomy_version"] == "2026-09-15.final57"
    assert len(pkg["categories"]) == 2 and len(pkg["products"]) == 2
    assert lc.check(pkg) == []


def test_read_package_missing_file(tmp_path) -> None:
    with pytest.raises(lc.PackageError, match="패키지 파일이 없다"):
        lc.read_package(tmp_path)


def test_check_catches_orphan_leaf(tmp_path) -> None:
    cats = [CATS[0], {**CATS[1], "parentSourceCategoryId": "GROUP-99"}]
    problems = lc.check(lc.read_package(_write(tmp_path, cats=cats)))
    assert any("부모가 없는 소분류" in p for p in problems)


def test_check_catches_duplicate_and_unknown_category(tmp_path) -> None:
    prods = [_product("KAKAO_GIFT:1"), _product("KAKAO_GIFT:1"), _product("KAKAO_GIFT:3", cat="CAT-99-99")]
    problems = lc.check(lc.read_package(_write(tmp_path, prods=prods)))
    assert any("sourceProductId 중복" in p for p in problems)
    assert any("소분류에 없는 카테고리" in p for p in problems)


def test_check_catches_declared_count_mismatch(tmp_path) -> None:
    problems = lc.check(lc.read_package(_write(tmp_path, count=9999)))
    assert any("summary.productCount=9999" in p for p in problems)
