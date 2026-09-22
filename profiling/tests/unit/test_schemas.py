"""api/schemas DTO 경계 — 문서 1 §7.6·7.7 규칙이 Pydantic으로 지켜지는지."""

import pytest
from pydantic import ValidationError

from profiling.transport.schemas import ProfileCallbackRequest, ProfileExtractRequest

BASE = {"recipientUserId": 1, "sourceVersion": 0, "dislikedCategories": [], "giftPreference": None, "reviews": []}


def test_extract_request_minimal_v1() -> None:
    req = ProfileExtractRequest(**BASE)
    assert req.giftPreference is None and req.reviews == [] and req.dislikedCategories == []


@pytest.mark.parametrize("patch", [
    {"reviews": [{"productId": i, "rating": 3} for i in range(1, 12)]},        # 11개
    {"reviews": [{"productId": 1, "rating": 0}]},                                # rating 범위
    {"reviews": [{"productId": 1, "rating": 6}]},
    {"recipientUserId": 0},                                                      # 양의 정수
    {"sourceVersion": -1},
    {"unknownField": 1},                                                         # extra="forbid"
    {"dislikedCategories": [{"categoryId": 1}]},                                 # categoryName 필수
])
def test_extract_request_rejects(patch) -> None:
    with pytest.raises(ValidationError):
        ProfileExtractRequest(**{**BASE, **patch})


def test_extract_request_gift_preference_required_key() -> None:
    with pytest.raises(ValidationError):                                         # "없으면 null" — 키 자체를 빼면 안 된다
        ProfileExtractRequest(**{k: v for k, v in BASE.items() if k != "giftPreference"})


def test_callback_request_no_tags_and_max_30() -> None:
    ok = ProfileCallbackRequest(recipientUserId=1, sourceVersion=0, recommendedProductIds=list(range(30)))
    assert ok.profileStatus == "COMPLETED"
    with pytest.raises(ValidationError):
        ProfileCallbackRequest(recipientUserId=1, sourceVersion=0, recommendedProductIds=list(range(31)))
    with pytest.raises(ValidationError):                                         # DR-035: 태그 필드 금지
        ProfileCallbackRequest(recipientUserId=1, sourceVersion=0, recommendedProductIds=[], preferredTags=["a"])
