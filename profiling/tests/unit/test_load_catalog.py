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


# ---------------------------------------------------------------- 합본 파일(서버 적재용, 10-15)

PIDS = {"KAKAO_GIFT:1": 11, "KAKAO_GIFT:2": 7}
CIDS = {"GROUP-01": 1, "CAT-01-01": 11}


def _bundle(tmp_path, metrics=None):
    return lc.build_bundle(lc.read_package(_write(tmp_path)), PIDS, CIDS, metrics)


def test_build_bundle_uses_backend_ids_in_order(tmp_path) -> None:
    b = _bundle(tmp_path, {"KAKAO_GIFT:1": {"availability": "available", "viewCount": 5}})
    assert b["format"] == lc.BUNDLE_FORMAT and b["packageId"] == "test-pkg"
    assert [c["id"] for c in b["categories"]] == [1, 11] and b["categories"][1]["parentId"] == 1
    assert [p["id"] for p in b["products"]] == [7, 11]                     # 번호 오름차순 — 같은 입력이면 같은 파일
    by = {p["id"]: p for p in b["products"]}
    assert by[11]["categoryId"] == 11 and by[11]["sourceProductId"] == "KAKAO_GIFT:1"
    assert (by[11]["availability"], by[11]["viewCount"]) == ("available", 5) and by[7]["availability"] is None
    assert lc.check_bundle(b) == []


def test_build_bundle_refuses_missing_backend_id(tmp_path) -> None:
    with pytest.raises(lc.PackageError, match="Backend 번호가 없는 상품 1건"):
        lc.build_bundle(lc.read_package(_write(tmp_path)), {"KAKAO_GIFT:1": 11}, CIDS)


@pytest.mark.parametrize("name", ["b.json", "b.json.gz"])
def test_bundle_roundtrip_json_and_gzip(tmp_path, name) -> None:
    b = _bundle(tmp_path / "pkg")
    path = tmp_path / name
    lc.write_bundle(b, path)
    assert lc.read_bundle(str(path)) == b
    first = path.read_bytes()
    lc.write_bundle(b, path)
    assert path.read_bytes() == first                                      # gzip 도 시각 고정 — 같은 바이트


def test_read_bundle_from_s3_uses_s3_get(tmp_path, monkeypatch) -> None:
    b = _bundle(tmp_path)
    seen = []
    monkeypatch.setattr(lc, "_s3_get", lambda uri: seen.append(uri) or json.dumps(b).encode())
    assert lc.read_bundle("s3://bucket/catalog/b.json") == b and seen == ["s3://bucket/catalog/b.json"]


def test_read_bundle_rejects_other_json(tmp_path) -> None:
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"products": []}), encoding="utf-8")
    with pytest.raises(lc.PackageError, match="합본 형식이 아니다"):
        lc.read_bundle(str(path))


def test_check_bundle_finds_bad_hierarchy_and_duplicates(tmp_path) -> None:
    b = _bundle(tmp_path)
    b["products"][1]["id"] = b["products"][0]["id"]
    b["products"][0]["categoryId"] = 1                                     # 대분류를 가리킴
    b["categories"][1]["parentId"] = None                                  # 소분류인데 부모 없음
    problems = " ".join(lc.check_bundle(b))
    assert "상품 번호가 겹친다" in problems and "계층이 맞지 않는" in problems and "소분류에 없는" in problems


def test_cli_bundle_alone(tmp_path) -> None:
    with pytest.raises(SystemExit):
        lc.main(["--bundle", "x.json", "--metrics", "m.jsonl"])
