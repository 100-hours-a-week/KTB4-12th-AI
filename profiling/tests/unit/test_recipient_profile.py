"""profile/recipient_profile — 태그 보관·갱신 규칙.

채운 것: from_outcome · should_replace · cap_tags (09-22, DB 연결에 필요).
남은 것(v3, skip): merge_explicit_dislikes · to_search_hint.
"""

from uuid import UUID

import pytest

from profiling import types as rp
from profiling.types import (
    DislikedCategory,
    ProfileOutcome,
    ProfileRequest,
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


@pytest.mark.skip(reason="v3 — merge_explicit_dislikes 채운 뒤 활성화")
def test_merge_explicit_dislikes_front_and_dedup() -> None:
    raise NotImplementedError


@pytest.mark.skip(reason="v3 — Search 인자 합의 뒤 활성화")
def test_to_search_hint_shape() -> None:
    raise NotImplementedError

