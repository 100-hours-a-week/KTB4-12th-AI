"""profile/recipient_profile — 태그 보관·갱신 규칙.

채운 것: from_outcome · should_replace (09-22, DB 연결에 필요).
리뷰 신호(10-08, 규칙표_별점x글 §3·§4): rating_score · CategoryScore.bias · score_categories · review_hash · to_review_records · from_outcome 의 취향·리뷰·점수.
선호 태그 모양(10-13 DR-040): preferred_tags = 추천에 쓴 태그 그대로(축 · 출처 · 근거 자리, 자르지 않음) · 비선호는 disliked_categories 하나.
남은 것(v3, skip): to_search_hint.
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


def _keep(tag: str, axis4: str = "카테고리", ref: str = "pref") -> rp.Decision:
    return rp.Decision(tag=tag, axis="likes", decision="keep", ref=ref, axis4=axis4)


def _outcome(rq, preferred=(), disliked=("출산·육아용품",)) -> ProfileOutcome:
    """preferred: 검증기가 남긴(keep) 태그 글자들 — 판정 로그에 keep 으로 넣는다(profile_tags 가 로그에서 만든다)."""
    return ProfileOutcome(
        recipient_user_id=rq.recipient_user_id, source_version=rq.source_version, status=RunStatus.RESULT_READY, input_hash="h",
        validation=ValidationResult(likes=[], key_features=[], dislikes=[], preferred_tags=list(preferred), disliked_tags=list(disliked),
                                    log=[_keep(t) for t in preferred]),
        search=SearchResult(product_ids=[3, 1, 2], query_text="", catalog_version_id=CV), validator_version="v1-skip",
    )


def test_from_outcome_maps_fields() -> None:
    rq = _rq()
    row = rp.from_outcome(rq, _outcome(rq), profile_run_id="run-1")
    assert (row.recipient_user_id, row.source_version, row.profile_run_id) == (9073, 3, "run-1")
    assert row.preferred_tags == [] and not hasattr(row, "disliked_tags")     # 비선호 이름 사본 열은 없다(DR-040)
    assert row.disliked_categories == rq.disliked_categories                    # 분석 시점 사본 — 비선호는 이것 하나
    assert row.recommended_product_ids == [3, 1, 2] and row.catalog_version_id == CV   # 콜백과 같은 순서
    assert row.axes == {"likes": [], "key_features": [], "dislikes": []} and row.validator_version == "v1-skip"


def test_from_outcome_keeps_every_tag_used_for_the_pool() -> None:
    """12개로 자르던 때(10-13 전)는 태그가 12개를 넘으면 저장된 태그와 추천 30개를 고를 때 쓴 태그가 달랐다 — 이제 그대로(DR-040)."""
    rq = _rq()
    tags = [f"t{i:02d}" for i in range(20)]
    row = rp.from_outcome(rq, _outcome(rq, preferred=tags))
    assert [(t.axis, t.tag, t.source, t.ref) for t in row.preferred_tags] == [("카테고리", t, "preference", "pref") for t in tags]


def test_should_replace_by_source_version() -> None:
    cur = rp.RecipientProfile(recipient_user_id=1, source_version=3)
    assert rp.should_replace(None, rp.RecipientProfile(recipient_user_id=1, source_version=1))       # 기존 없음
    assert rp.should_replace(cur, rp.RecipientProfile(recipient_user_id=1, source_version=3))        # 같음 → 재분석 결과로 덮음
    assert rp.should_replace(cur, rp.RecipientProfile(recipient_user_id=1, source_version=4))        # 높음
    assert not rp.should_replace(cur, rp.RecipientProfile(recipient_user_id=1, source_version=2))    # 낮음 → 순서 역전, 유지


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
    """상품당 앞의 것(최신)만 · 해시는 원문 기준 · 글은 정리본(text_clean)으로만 행에 남는다(10-13 DR-039)."""
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
    assert row.gift_preference_clean == "향수 좋아해요" and [r.text_clean for r in row.reviews] == ["좋다", None]   # 정리본(DR-039)
    assert row.guard_version == rp.guard.GUARD_VERSION


def test_from_outcome_stores_only_the_guard_cleaned_text() -> None:
    """원문 그대로는 없고 정리본만(10-13 DR-039) — 개인정보 · 욕설은 가린 글, 의미 없는 글은 None. 지문(해시)은 원문 기준."""
    raw_pref, raw_review = "010-1234-5678 로 연락 주세요 향수 좋아해요", "시1발 배송은 느림 그래도 향은 좋아"
    rq = ProfileRequest(recipient_user_id=9, source_version=1, gift_preference=raw_pref, disliked_categories=[],
                        reviews=_reviews((1, 5, raw_review), (2, 4, "ㅋㅋㅋㅋㅋㅋ")))
    row = rp.from_outcome(rq, _outcome(rq, disliked=()))
    dumped = row.model_dump_json()
    assert "010-1234-5678" not in dumped and "시1발" not in dumped                          # 원문의 개인정보 · 욕설은 어디에도 없다
    assert row.gift_preference_clean == "[연락처] 로 연락 주세요 향수 좋아해요"
    assert [r.text_clean for r in row.reviews] == ["[가림] 배송은 느림 그래도 향은 좋아", None]   # 의미 없는 글은 None
    assert row.gift_preference_hash == rp.preference_hash(raw_pref)                       # 지문은 원문 기준(바뀜 판정)


def test_profile_tags_carry_axis_source_and_ref() -> None:
    v = rp.ValidationResult(likes=[], key_features=[], dislikes=[], preferred_tags=["향수", "조말론"], disliked_tags=[], log=[
        rp.Decision(tag="향수", axis="likes", decision="keep", ref="r1", axis4="카테고리"),
        rp.Decision(tag="조말론", axis="likes", decision="keep", ref="pref, r1", axis4="브랜드"),
        rp.Decision(tag="쿠폰", axis="likes", decision="drop", ref="r1", axis4="카테고리", reason="판촉 단어")])
    tags = rp.profile_tags(v)
    assert [(t.axis, t.tag, t.source, t.ref) for t in tags] == [("카테고리", "향수", "review_text", "r1"), ("브랜드", "조말론", "preference", "pref, r1")]
    assert rp.profile_tags(None) == []


def test_from_outcome_blank_preference_is_none_and_defaults_empty() -> None:
    rq = ProfileRequest(recipient_user_id=1, source_version=1, gift_preference="  ", disliked_categories=[], reviews=[])
    row = rp.from_outcome(rq, _outcome(rq, disliked=()))
    assert row.gift_preference_hash is None and row.category_scores == [] and row.reviews == []


@pytest.mark.skip(reason="v3 — Search 인자 합의 뒤 활성화")
def test_to_search_hint_shape() -> None:
    raise NotImplementedError

