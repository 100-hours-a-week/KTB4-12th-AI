"""tools/catalog/import_be_ids 의 매칭 — 파일도 DB도 없이 (순수 함수만).

Backend 회신에는 수집처 ID가 없어 이름으로 잇는다. 이름이 겹칠 때 **항상 같은 배정**이 나오는지,
한 Backend 번호가 두 상품에 붙는 사고를 잡아내는지가 핵심이다.
"""

import pytest

from tools.catalog import import_be_ids as imp


def _ours(sid: str, name: str, brand: str = "b", price: int = 1000, leaf: str = "메이크업") -> dict:
    return {"source_product_id": sid, "name": name, "brand": brand, "unit_price": price, "leaf_name": leaf}


def _theirs(bid: int, name: str, brand: str = "b", price: int = 1000, leaf: str = "메이크업",
            stock: int = 100, views: int = 0) -> dict:
    return {"backend_id": bid, "name": name, "brand": brand, "price": price, "leaf_name": leaf,
            "stock": stock, "views": views}


def test_matches_by_name_brand_price_category() -> None:
    rep = imp.match_products([_ours("S:1", "립밤"), _ours("S:2", "머그컵", leaf="주방")],
                             [_theirs(11, "머그컵", leaf="주방"), _theirs(10, "립밤")])
    assert rep.pairs == {"S:1": 10, "S:2": 11}
    assert rep.exact == 2 and rep.tied == 0 and not rep.problems
    assert rep.unmatched_ours == [] and rep.unmatched_theirs == []


def test_same_name_different_price_is_not_confused() -> None:
    """이름·브랜드가 같아도 가격이 다르면 다른 상품 — 키에 가격이 들어 있다."""
    rep = imp.match_products([_ours("S:1", "타이", price=100), _ours("S:2", "타이", price=200)],
                             [_theirs(7, "타이", price=200), _theirs(9, "타이", price=100)])
    assert rep.pairs == {"S:1": 9, "S:2": 7} and rep.exact == 2


def test_true_duplicates_are_paired_deterministically() -> None:
    """네 값이 모두 같은 진짜 중복 — 정렬해 1:1로 붙이고, 다시 돌려도 같은 결과여야 한다."""
    ours = [_ours("S:3", "상품권"), _ours("S:1", "상품권"), _ours("S:2", "상품권")]
    theirs = [_theirs(30, "상품권"), _theirs(10, "상품권"), _theirs(20, "상품권")]
    first = imp.match_products(ours, theirs)
    assert first.pairs == {"S:1": 10, "S:2": 20, "S:3": 30}      # 양쪽 오름차순으로 짝
    assert first.tied == 3 and first.exact == 0 and not first.problems
    assert imp.match_products(list(reversed(ours)), list(reversed(theirs))).pairs == first.pairs


def test_reports_unmatched_on_both_sides() -> None:
    rep = imp.match_products([_ours("S:1", "있음"), _ours("S:2", "우리에만")],
                             [_theirs(10, "있음"), _theirs(99, "회신에만")])
    assert rep.pairs == {"S:1": 10}
    assert rep.unmatched_ours == ["S:2"] and rep.unmatched_theirs == [99]


def test_extra_duplicate_on_one_side_is_reported_not_reused() -> None:
    """회신이 2건인데 우리가 1건이면 남는 번호는 쓰지 않는다 — 번호를 돌려 쓰면 다른 상품을 추천하게 된다."""
    rep = imp.match_products([_ours("S:1", "타이")], [_theirs(10, "타이"), _theirs(11, "타이")])
    assert rep.pairs == {"S:1": 10} and rep.unmatched_theirs == [11] and not rep.problems


def test_categories_matched_by_name_per_level() -> None:
    ours = [{"source_category_id": "GROUP-01", "name": "뷰티", "level": 1},
            {"source_category_id": "CAT-01-02", "name": "메이크업", "level": 2}]
    theirs = [{"top_id": 1, "top_name": "뷰티", "leaf_id": 12, "leaf_name": "메이크업"}]
    pairs, problems = imp.match_categories(ours, theirs)
    assert pairs == {"GROUP-01": 1, "CAT-01-02": 12} and problems == []


def test_missing_category_name_is_a_problem() -> None:
    ours = [{"source_category_id": "CAT-09-09", "name": "없는분류", "level": 2}]
    theirs = [{"top_id": 1, "top_name": "뷰티", "leaf_id": 12, "leaf_name": "메이크업"}]
    pairs, problems = imp.match_categories(ours, theirs)
    assert pairs == {} and any("없는분류" in p for p in problems)


@pytest.mark.parametrize(("stock", "expected"), [(100, "available"), (1, "available"), (0, "unavailable")])
def test_availability_from_stock(stock, expected) -> None:
    assert imp.availability_of(stock) == expected
