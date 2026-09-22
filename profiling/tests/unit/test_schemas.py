"""api/schemas DTO 경계 — 문서 1 §7.6·7.7 규칙이 Pydantic으로 지켜지는지."""

import pytest
from pydantic import ValidationError

from profiling.transport.schemas import (
    ProductRecord,
    ProfileCallbackRequest,
    ProfileExtractRequest,
    unknown_fields,
)

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
    {"dislikedCategories": [{"categoryId": 1}]},                                 # categoryName 필수
])
def test_extract_request_rejects(patch) -> None:
    with pytest.raises(ValidationError):
        ProfileExtractRequest(**{**BASE, **patch})


def test_extract_request_v1_three_fields_only() -> None:
    """v1 BE는 giftPreference·reviews 없이 세 필드만 보낸다 — 키가 없으면 null·[]."""
    req = ProfileExtractRequest(recipientUserId=1, sourceVersion=0, dislikedCategories=[{"categoryId": 12, "categoryName": "캠핑용품"}])
    assert req.giftPreference is None and req.reviews == []
    with pytest.raises(ValidationError):                                         # dislikedCategories는 v1 핵심 — 키 생략 불가
        ProfileExtractRequest(recipientUserId=1, sourceVersion=0)


def test_callback_request_no_tags_and_max_30() -> None:
    ok = ProfileCallbackRequest(recipientUserId=1, sourceVersion=0, recommendedProductIds=list(range(30)))
    assert ok.profileStatus == "COMPLETED"
    with pytest.raises(ValidationError):
        ProfileCallbackRequest(recipientUserId=1, sourceVersion=0, recommendedProductIds=list(range(31)))
    with pytest.raises(ValidationError):                                         # DR-035: 태그 필드 금지
        ProfileCallbackRequest(recipientUserId=1, sourceVersion=0, recommendedProductIds=[], preferredTags=["a"])


def test_extract_request_unknown_fields_are_kept_and_reported() -> None:
    """계약에 없는 필드는 400이 아니라 무시 + 보고 (CONTRACT_7_6_UNKNOWN_FIELD). Backend가 필드를 추가해도 연동이 안 깨진다."""
    req = ProfileExtractRequest(**{**BASE, "extraTop": 1,
                                   "dislikedCategories": [{"categoryId": 1, "categoryName": "a", "weight": 0.5}],
                                   "reviews": [{"productId": 1, "rating": 3, "createdAt": "x"}]})
    assert unknown_fields(req) == {"": ["extraTop"], "dislikedCategories": ["weight"], "reviews": ["createdAt"]}
    assert unknown_fields(ProfileExtractRequest(**BASE)) == {}


def test_product_record_ignores_unknown_and_accepts_view_aliases() -> None:
    base = {"productId": 1, "name": "n", "brand": "b", "description": None, "categoryId": 1, "categoryName": "c",
            "price": 0, "available": True, "updatedAt": "2026-09-01T00:00:00Z"}
    assert ProductRecord(**{**base, "sales": 3, "imageUrl": "x"}).productId == 1          # 모르는 필드 무시
    assert ProductRecord(**{**base, "views": 42}).viewCount == 42                         # Backend 열 이름
    assert ProductRecord(**{**base, "viewCount": 7}).viewCount == 7
    assert ProductRecord(**base).viewCount == 0                                           # 없으면 0
    with pytest.raises(ValidationError):                                                  # 필수 누락은 여전히 오류
        ProductRecord(**{k: v for k, v in base.items() if k != "price"})
