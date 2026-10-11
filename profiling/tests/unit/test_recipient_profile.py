"""profile/recipient_profile — 태그 보관·갱신 규칙.

채운 것: from_outcome · should_replace · cap_tags (09-22, DB 연결에 필요).
리뷰 신호(10-08, 규칙표_별점x글 §3·§4): rating_score · CategoryScore.bias · score_categories · review_hash · to_review_records · from_outcome 의 취향·리뷰·점수.
남은 것(v3, skip): merge_explicit_dislikes · to_search_hint.
"""

from uuid import UUID

import pytest

from profiling import types as rp
from profiling.types import (
    DislikedCategory,
    ProfileOutcome,
    ProfileRequest,
    Review,
    RunStatus,
    SearchResult,
    ValidationResult,
)

CV = UUID(int=1)


def _rq(disliked=((802, "출산·육아용품"),)) -> ProfileRequest:
    return ProfileRequest(recipient_user_id=9073, source_version=3, gift_preference=None,
                          disliked_categories=[DislikedCategory(category_id=i, category_name=n) for i, n in disliked], reviews=[])


def _outcome(rq, preferred=(), disliked=("출산·육아용품",)) -> ProfileOutcome:
    return ProfileOutcome(
        recipient_user_id=rq.recipient_user_id, source_version=rq.source_version, status=RunStatus.RESULT_READY, input_hash="h",
        validation=ValidationResult(likes=[], key_features=[], dislikes=[], preferred_tags=list(preferred), disliked_tags=list(disliked), log=[]),
        search=SearchResult(product_ids=[3, 1, 2], query_text="", catalog_version_id=CV), validator_version="v1-skip",
    )


def test_from_outcome_maps_fields() -> None:
    rq = _rq()
    row = rp.from_outcome(rq, _outcome(rq), profile_run_id="run-1")
    assert (row.recipient_user_id, row.source_version, row.profile_run_id) == (9073, 3, "run-1")
    assert row.preferred_tags == [] and row.disliked_tags == ["출산·육아용품"]
    assert row.disliked_categories == rq.disliked_categories                    # 분석 시점 사본
    assert row.recommended_product_ids == [3, 1, 2] and row.catalog_version_id == CV   # 콜백과 같은 순서
    assert row.axes == {"likes": [], "key_features": [], "dislikes": []} and row.validator_version == "v1-skip"


def test_from_outcome_caps_tags() -> None:
    rq = _rq()
    row = rp.from_outcome(rq, _outcome(rq, preferred=[f"t{i}" for i in range(20)], disliked=["a", "a", "b"] + [f"d{i}" for i in range(10)]))
    assert len(row.preferred_tags) == rp.MAX_PREFERRED_TAGS and row.disliked_tags[:2] == ["a", "b"] and len(row.disliked_tags) == rp.MAX_DISLIKED_TAGS


def test_should_replace_by_source_version() -> None:
    cur = rp.RecipientProfile(recipient_user_id=1, source_version=3)
    assert rp.should_replace(None, rp.RecipientProfile(recipient_user_id=1, source_version=1))       # 기존 없음
    assert rp.should_replace(cur, rp.RecipientProfile(recipient_user_id=1, source_version=3))        # 같음 → 재분석 결과로 덮음
    assert rp.should_replace(cur, rp.RecipientProfile(recipient_user_id=1, source_version=4))        # 높음
    assert not rp.should_replace(cur, rp.RecipientProfile(recipient_user_id=1, source_version=2))    # 낮음 → 순서 역전, 유지


def test_cap_tags() -> None:
    assert rp.cap_tags([], 1) == []
    assert rp.cap_tags(["a", " a ", "", "b", "c"], 2) == ["a", "b"]              # 공백 정리·중복 제거·상한


# ---------------------------------------------------------------- 리뷰 신호 — 별점 → 카테고리 편향 (규칙표 §3)


@pytest.mark.parametrize("rating,score", [(1, -2), (2, -1), (3, 0), (4, 1), (5, 2)])
def test_rating_score(rating, score) -> None:
    assert rp.rating_score(rating) == score


@pytest.mark.parametrize("ratings,bias", [
    ((), 0.0),                 # 리뷰 없음 → 0 ÷ 2
    ((1,), -2 / 3),            # 1점 하나 → −0.67
    ((5,), 2 / 3),             # 5점 하나 → +0.67
    ((5, 1), 0.0),             # 상쇄
    ((5, 5), 1.0),
    ((1, 1), -1.0),
    ((5, 5, 1), 0.4),          # 2 ÷ 5
    ((1, 1, 1), -1.2),         # −6 ÷ 5
])
def test_category_bias_table(ratings, bias) -> None:
    """규칙표 §3 결과표 8행 그대로 — 편향 = 점수 합 ÷ (n + 2)."""
    cs = rp.CategoryScore(category_id=11, n=len(ratings), score_sum=sum(rp.rating_score(r) for r in ratings))
    assert cs.bias == pytest.approx(bias)


def _reviews(*items) -> list[Review]:
    """(product_id, rating[, text]) 튜플들 → Review 목록. 글이 없으면 None."""
    return [Review(product_id=p, rating=r, review_text=(t[0] if t else None)) for p, r, *t in items]


CATEGORY_OF = {1: 11, 2: 11, 3: 12, 4: 12}      # 상품 → 소분류. 5는 카탈로그에 없음


def test_score_categories_groups_by_category_and_sorts() -> None:
    out = rp.score_categories(_reviews((3, 1), (1, 5), (4, 3), (2, 5)), CATEGORY_OF)
    assert [(c.category_id, c.n, c.score_sum) for c in out] == [(11, 2, 4), (12, 2, -2)]     # id 오름차순 · 3점은 0점으로 n만 늘림
    assert [round(c.bias, 2) for c in out] == [1.0, -0.5]                                     # 4 ÷ (2+2) · −2 ÷ (2+2)


def test_score_categories_skips_unknown_product_and_duplicate() -> None:
    out = rp.score_categories(_reviews((5, 1), (1, 4), (1, 1)), CATEGORY_OF)                 # 5: 카탈로그 없음 · 상품 1 두 번: 앞(최신)만
    assert [(c.category_id, c.n, c.score_sum) for c in out] == [(11, 1, 1)]


def test_score_categories_empty() -> None:
    assert rp.score_categories([], CATEGORY_OF) == []


# ---------------------------------------------------------------- 리뷰 행 · 해시 (규칙표 §4 · 저장 설계)


def test_review_hash_is_stable_and_normalizes_text() -> None:
    a = rp.review_hash(Review(product_id=1, rating=5, review_text="향이  좋아요 "))
    assert len(a) == 64 and a == rp.review_hash(Review(product_id=1, rating=5, review_text="향이 좋아요"))        # 공백만 다름 → 같음
    assert a != rp.review_hash(Review(product_id=1, rating=4, review_text="향이 좋아요"))                           # 별점 다름 → 다른 리뷰
    assert a != rp.review_hash(Review(product_id=2, rating=5, review_text="향이 좋아요"))                           # 상품 다름
    assert rp.review_hash(Review(product_id=1, rating=5)) == rp.review_hash(Review(product_id=1, rating=5, review_text="  "))   # 없음 = 공백


def test_to_review_records_keeps_first_per_product_without_text() -> None:
    """상품당 앞의 것(최신)만 · 글은 행에 없고 해시에만 들어간다(10-11 결정 B — 원문 저장 안 함)."""
    rows = rp.to_review_records(_reviews((7, 5, " "), (8, 2, "별로"), (7, 1, "두 번째")))
    assert [(r.product_id, r.rating, r.position) for r in rows] == [(7, 5, 0), (8, 2, 1)]
    assert rows[0].review_hash == rp.review_hash(Review(product_id=7, rating=5))
    assert rows[1].review_hash == rp.review_hash(Review(product_id=8, rating=2, review_text="별로"))
    assert "review_text" not in rp.ReviewRecord.model_fields                   # 원문 칸 자체가 없다


def test_review_record_position_capped_at_nine() -> None:
    """자리는 0~9 — 7.6 상한 10개, DB CHECK(0005)와 같다."""
    with pytest.raises(ValueError):
        rp.ReviewRecord(product_id=1, rating=5, review_hash="a" * 64, position=10)


def test_preference_hash_normalizes_and_blank_is_none() -> None:
    a = rp.preference_hash("향수  좋아해요 ")
    assert len(a) == 64 and a == rp.preference_hash("향수 좋아해요") and a != rp.preference_hash("향수 싫어요")
    assert rp.preference_hash(None) is None and rp.preference_hash("   ") is None


def test_from_outcome_carries_preference_reviews_and_scores() -> None:
    rq = ProfileRequest(recipient_user_id=9073, source_version=3, gift_preference="향수 좋아해요", disliked_categories=[],
                        reviews=_reviews((1, 5, "좋다"), (3, 1)))
    scores = [rp.CategoryScore(category_id=11, n=1, score_sum=2), rp.CategoryScore(category_id=12, n=1, score_sum=-2)]
    row = rp.from_outcome(rq, _outcome(rq, disliked=()), category_scores=scores)
    assert row.gift_preference_hash == rp.preference_hash("향수 좋아해요") and row.category_scores == scores
    assert [(r.product_id, r.rating, r.position) for r in row.reviews] == [(1, 5, 0), (3, 1, 1)]
    assert "향수 좋아해요" not in row.model_dump_json() and "좋다" not in row.model_dump_json()   # 원문이 행 어디에도 없다


def test_from_outcome_blank_preference_is_none_and_defaults_empty() -> None:
    rq = ProfileRequest(recipient_user_id=1, source_version=1, gift_preference="  ", disliked_categories=[], reviews=[])
    row = rp.from_outcome(rq, _outcome(rq, disliked=()))
    assert row.gift_preference_hash is None and row.category_scores == [] and row.reviews == []


@pytest.mark.skip(reason="v3 — merge_explicit_dislikes 채운 뒤 활성화")
def test_merge_explicit_dislikes_front_and_dedup() -> None:
    raise NotImplementedError


@pytest.mark.skip(reason="v3 — Search 인자 합의 뒤 활성화")
def test_to_search_hint_shape() -> None:
    raise NotImplementedError

