"""tools/catalog/fetch_export — 7.9 계약 점검·ID 대조·저장. HTTP 없이 dict/파일로."""

import json
from pathlib import Path

from profiling.profile.types import ErrorCode
from tools.catalog import fetch_export as fx

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "catalog_sample.json"


def _doc(products, generated="2026-09-22T00:00:00Z"):
    return {"message": "ok", "data": {"generatedAt": generated, "products": products}}


def _p(pid, **over):
    base = {"productId": pid, "name": f"n{pid}", "brand": "b", "description": None, "categoryId": 1, "categoryName": "c",
            "price": 10, "available": True, "updatedAt": "2026-09-01T00:00:00Z"}
    return {**base, **over}


def test_contract_ok_with_backend_names_and_unknown_fields() -> None:
    rep = fx.check_contract(_doc([_p(1, views=5, sales=2), _p(2, views=9, imageUrl="x")]))
    assert rep.ok and rep.total == 2 and len(rep.valid) == 2 and rep.code is None
    assert rep.view_field == "views"                                                    # Backend 열 이름으로 왔음을 보고
    assert rep.unknown_fields == {"sales": 1, "imageUrl": 1}                            # 무시하되 보고
    assert [p.viewCount for p in rep.valid] == [5, 9]                                   # 별칭 수용


def test_contract_reports_missing_required_and_does_not_pass() -> None:
    bad = {k: v for k, v in _p(3).items() if k != "price"}
    rep = fx.check_contract(_doc([_p(1), bad, _p(2, categoryId="x")]))
    assert not rep.ok and rep.code is ErrorCode.CONTRACT_7_9_SCHEMA
    assert len(rep.valid) == 1 and len(rep.invalid) == 2
    assert rep.missing_fields == {"price": 1}
    assert rep.invalid[0][1].startswith("price") and rep.invalid[1][1].startswith("categoryId")


def test_contract_envelope_error() -> None:
    rep = fx.check_contract({"message": "ok", "data": {"items": []}})
    assert not rep.ok and "data.products" in rep.envelope_error


def test_compare_ids_and_changes() -> None:
    ai = fx.check_contract(_doc([_p(1), _p(2), _p(3, categoryId=7, categoryName="old")])).valid
    be = fx.check_contract(_doc([_p(2), _p(3, categoryId=8, categoryName="new", available=False), _p(4)])).valid
    d = fx.compare(ai, be)
    assert d.only_in_ai == [1] and d.only_in_backend == [4] and d.common == 2 and not d.clean
    assert {(pid, f) for pid, f, _, _ in d.changed} == {(3, "categoryId"), (3, "categoryName"), (3, "available")}
    assert fx.compare(ai, ai).clean


def test_main_from_file_saves_normalized_document(tmp_path: Path, capsys) -> None:
    src = tmp_path / "export.json"
    src.write_text(json.dumps(_doc([_p(1, views=3, sales=1), _p(2)])), encoding="utf-8")
    out = tmp_path / "data" / "catalog_export.json"
    code = fx.main(["--from-file", str(src), "--out", str(out), "--compare", str(FIXTURE)])
    assert code == 2                                                                    # 예시 카탈로그(111건)와 다르므로 차이 있음
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert [p["productId"] for p in saved["data"]["products"]] == [1, 2]
    assert saved["data"]["products"][0]["viewCount"] == 3 and "sales" not in saved["data"]["products"][0]   # 계약 이름으로 정규화
    text = capsys.readouterr().out
    assert "계약에 없는 필드(무시함): sales×1" in text and "AI에만" in text and "Backend에만" in text


def test_main_identical_to_current_catalog_is_clean(tmp_path: Path) -> None:
    out = tmp_path / "c.json"
    assert fx.main(["--from-file", str(FIXTURE), "--out", str(out), "--compare", str(FIXTURE)]) == 0
    assert out.is_file()


def test_main_contract_violation_does_not_save(tmp_path: Path, capsys) -> None:
    src = tmp_path / "bad.json"
    src.write_text(json.dumps(_doc([{k: v for k, v in _p(1).items() if k != "name"}])), encoding="utf-8")
    out = tmp_path / "c.json"
    assert fx.main(["--from-file", str(src), "--out", str(out), "--compare", "none"]) == 1
    assert not out.exists() and "CONTRACT_7_9_SCHEMA" in capsys.readouterr().out
