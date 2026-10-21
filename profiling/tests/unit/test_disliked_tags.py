"""비선호 태그(tag3disliked — v2 운영 규격, DR-043, 10-16) — "소니 싫어" 같은 글 속 부정을 비선호 태그로 뽑아 후보에서 뺀다.

규격 · 스키마 · 검증기(극성) · ValidationResult(dislikes 칸) · DB 선호 칸에 안 섞임 · 상품 대조 · 풀 고르기에서 제외.
"""

from datetime import UTC, datetime

from profiling import changes, pipeline, selection, tagging
from profiling.schemas import ProductRecord
from profiling.selection import Tag
from profiling.types import ProfileRequest, ProfileTag, RecipientProfile, profile_tags

DIS = tagging.load_spec("tag3disliked")
T3 = tagging.load_spec("tag3")


def _p(pid, *, brand="b", name=None, kind=None, cat="c1", cat_id=1, views=100) -> ProductRecord:
    return ProductRecord(productId=pid, name=name or f"상품 {pid}", brand=brand, description=None, categoryId=cat_id, categoryName=cat,
                         parentCategoryId=1, parentCategoryName="대1", productKind=kind, price=1000, availability="available",
                         updatedAt=datetime(2026, 10, 1, tzinfo=UTC), viewCount=views)


def test_spec_disliked_adds_polarity_and_keeps_tag3_unchanged():
    item = DIS.schema["properties"]["tags"]["items"]
    assert item["properties"]["polarity"]["enum"] == ["선호", "비선호"] and "polarity" in item["required"]
    assert "polarity" not in T3.schema["properties"]["tags"]["items"]["properties"]      # v2 운영 규격은 그대로(판 이름도 그대로)
    assert DIS.axes == T3.axes and DIS.disliked and not T3.disliked
    assert DIS.version.startswith("tag3disliked-") and DIS.version != T3.version


def test_validate_disliked_from_low_rating_review_but_positive_still_needs_4_5():
    tags = [{"axis": "브랜드", "tag": "소니", "ref": "pref", "polarity": "비선호"},
            {"axis": "카테고리", "tag": "향수", "ref": "r1", "polarity": "비선호"},       # 1점 리뷰의 '싫다' — 비선호는 별점 무관
            {"axis": "카테고리", "tag": "텀블러", "ref": "r1", "polarity": "선호"}]       # 1점 리뷰로 선호는 안 됨
    keep, drop = tagging.validate(tags, {"r1": 1}, True, DIS)
    assert [(t["tag"], t["polarity"]) for t in keep] == [("소니", "비선호"), ("향수", "비선호")]
    assert [t["tag"] for t in drop] == ["텀블러"]


def test_tag3_ignores_polarity_field():
    keep, _ = tagging.validate([{"axis": "브랜드", "tag": "소니", "ref": "pref", "polarity": "비선호"}], {}, True, T3)
    assert keep[0]["polarity"] == "선호"                  # 비선호 규격이 아니면 극성을 믿지 않는다


def test_validation_puts_disliked_in_dislikes_not_in_preferred():
    keep, drop = tagging.validate([{"axis": "IP", "tag": "산리오", "ref": "pref", "polarity": "선호"},
                                   {"axis": "브랜드", "tag": "소니", "ref": "pref", "polarity": "비선호"}], {}, True, DIS)
    v = tagging.to_validation(keep, drop, [])
    assert [t.tag for t in v.likes] == ["산리오"] and [t.tag for t in v.dislikes] == ["소니"]
    assert v.preferred_tags == ["산리오"]
    assert [t.text for t in tagging.tags_of(v)] == ["산리오"]
    assert tagging.disliked_tags_of(v) == [Tag("브랜드", "소니")]
    assert [t.tag for t in profile_tags(v)] == ["산리오"]          # DB 선호 칸(preferred_tags)엔 비선호가 안 들어간다


def test_disliked_hit_brand_name_kind_category():
    sony = _p(1, brand="소니", name="[소니공식스토어] SONY WH-ULT900N")
    ps = _p(2, brand="소니플레이스테이션", name="PS5 듀얼센스")
    hp = _p(3, brand="젠하이저", kind="헤드폰", cat="영상·음향기기")
    other = _p(4, brand="필립스", kind="전동칫솔", cat="생활가전")
    assert selection.disliked_hit(sony, Tag("브랜드", "소니")) and selection.disliked_hit(ps, Tag("브랜드", "소니"))
    assert selection.disliked_hit(sony, Tag("브랜드", "SONY"))                       # 상품명 영문
    assert selection.disliked_hit(hp, Tag("카테고리", "음향기기")) and selection.disliked_hit(hp, Tag("카테고리", "헤드폰"))
    assert not selection.disliked_hit(other, Tag("카테고리", "음향기기"))
    assert not selection.disliked_hit(other, Tag("브랜드", "필"))                     # 한 글자는 대조 안 함
    assert selection.disliked_hit(_p(5, name="산리오 쿠로미 우산"), Tag("IP", "쿠로미"))


def test_select_pool_never_picks_disliked_tag_products():
    products = [_p(1, brand="소니", views=900), _p(2, brand="소니", views=800), _p(3, kind="헤드폰", cat="영상·음향기기", views=700),
                *[_p(10 + k, views=100 - k, cat_id=2 + k % 5) for k in range(10)]]
    rq = ProfileRequest(recipient_user_id=1, source_version=1, gift_preference="x", disliked_categories=[], reviews=[])
    ranked = pipeline.rank_products(rq, products)
    sel = selection.select_pool(ranked, 5, allow_disliked=False, disliked_tags=[Tag("브랜드", "소니"), Tag("카테고리", "헤드폰")])
    assert sel.disliked_excluded == 3 and not {1, 2, 3} & set(sel.product_ids) and len(sel.product_ids) == 5
    assert sel.rule.endswith("+disliked")
    assert selection.select_pool(ranked, 5, allow_disliked=False).product_ids[:3] == [1, 2, 3]   # 비선호 태그가 없으면 인기순 그대로


def test_profile_tags_split_preferred_and_disliked_with_axis_and_ref():
    """DB 칸(0010) — 선호 keep 은 preferred_tags, 비선호 keep 은 disliked_tags. 같은 모양(축 · 출처 · 근거 자리)."""
    keep, drop = tagging.validate([{"axis": "IP", "tag": "산리오", "ref": "pref", "polarity": "선호"},
                                   {"axis": "브랜드", "tag": "소니", "ref": "pref", "polarity": "비선호"},
                                   {"axis": "카테고리", "tag": "향수", "ref": "r1", "polarity": "비선호"}], {"r1": 1}, True, DIS)
    v = tagging.to_validation(keep, drop, ["뷰티"])
    assert profile_tags(v) == [ProfileTag(axis="IP", tag="산리오", source="preference", ref="pref")]
    assert profile_tags(v, dislikes=True) == [ProfileTag(axis="브랜드", tag="소니", source="preference", ref="pref"),
                                              ProfileTag(axis="카테고리", tag="향수", source="review_text", ref="r1")]
    assert v.disliked_category_names == ["뷰티"]                    # BE 카테고리 이름(메모리) — DB 의 disliked_tags 와 다르다


def test_default_spec_is_tag3disliked_with_prompt():
    assert tagging.SPEC.name == "tag3disliked" and tagging.SPEC.disliked          # v2 운영 규격(DR-043)
    assert "비선호를 적는 법" in tagging.SYSTEM                               # 프롬프트 v2(10-16)


def test_change_log_counts_disliked_tags():
    def row(sv, dis):
        return RecipientProfile(recipient_user_id=1, source_version=sv,
                                disliked_tags=[ProfileTag(axis="브랜드", tag=t, source="preference", ref="pref") for t in dis])
    first = changes.log_fields(changes.summarize(None, row(1, ["소니"])))
    assert "disliked_tags=+1/-0" in first
    later = changes.log_fields(changes.summarize(row(1, ["소니"]), row(2, ["애플"])))
    assert "disliked_tags=+1/-1" in later and "소니" not in later and "애플" not in later   # 글자는 로그에 남기지 않는다


def test_dislikes_restating_be_categories_are_dropped_not_others():
    """W04 꼴 — BE 비선호 대분류(카페·디저트 · 반려동물)를 다시 말한 비선호는 버리고, 다른 비선호 · 선호는 그대로."""
    keep = [{"axis": "카테고리", "tag": "디저트", "ref": "pref", "polarity": "비선호"},
            {"axis": "카테고리", "tag": "반려동물 용품", "ref": "pref", "polarity": "비선호"},
            {"axis": "브랜드", "tag": "소니", "ref": "pref", "polarity": "비선호"},
            {"axis": "카테고리", "tag": "디저트", "ref": "r1", "polarity": "선호"}]
    k, d = tagging.drop_category_restatements(keep, [], ["카페·디저트", "반려동물"])
    assert [(t["tag"], t["polarity"]) for t in k] == [("소니", "비선호"), ("디저트", "선호")]
    assert [t["drop"] for t in d] == ["BE 비선호 대분류와 같음"] * 2
    assert tagging.drop_category_restatements(keep, [], []) == (keep, [])           # BE 비선호가 없으면 그대로

