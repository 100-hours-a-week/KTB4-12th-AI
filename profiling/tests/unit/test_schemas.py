"""api/schemas DTO 경계 — 문서 1 §7.6·7.7 규칙이 Pydantic으로 지켜지는지."""

import pytest
from pydantic import ValidationError

from profiling.schemas import (
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
    with pytest.raises(ValidationError):                                         # dislikedCategories 키 생략 불가 (10-09 전환 1단계)
        ProfileExtractRequest(recipientUserId=1, sourceVersion=0)
    with pytest.raises(ValidationError):                                         # 최대 5 — BE MAX_SELECTABLE_COUNT 와 같은 값(09-29). 6개면 400
        ProfileExtractRequest(recipientUserId=1, sourceVersion=0,
                              dislikedCategories=[{"categoryId": i, "categoryName": f"c{i}"} for i in range(1, 7)])


def test_extract_request_keys_required_values_may_be_empty() -> None:
    """10-09 결정: 입력 세 칸은 **키를 반드시** 보내고 값은 비어도 된다. 키가 빠지면 Backend 결함으로 보고 400(분석 안 함).

    전환 1단계(지금): dislikedCategories 키 필수. giftPreference·reviews 는 Backend 가 V2 를 배포할 때까지 생략 허용.
    전환 2단계(V2 배포 시): 아래 '생략 허용' 두 줄을 400 으로 뒤집고 schemas.py 의 default 를 지운다.
    """
    empty = ProfileExtractRequest(**BASE)                                        # 세 칸 전부 빈 값 — 접수(콜드 스타트, 인기순 30개)
    assert empty.dislikedCategories == [] and empty.giftPreference is None and empty.reviews == []
    for missing in ("recipientUserId", "sourceVersion", "dislikedCategories"):
        with pytest.raises(ValidationError):                                     # 1단계 키 필수 셋
            ProfileExtractRequest(**{k: v for k, v in BASE.items() if k != missing})
    only_three = ProfileExtractRequest(recipientUserId=1, sourceVersion=0, dislikedCategories=[])   # 지금 Backend 가 보내는 모양
    assert only_three.giftPreference is None and only_three.reviews == []       # 2단계에서 ValidationError 로 바뀐다


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


def test_product_record_availability_three_values() -> None:
    """7.9는 boolean available로 보내고 우리는 3값으로 받는다. 없거나 null이면 unknown — 추정하지 않는다."""
    base = {"productId": 1, "name": "n", "brand": "b", "description": None, "categoryId": 1, "categoryName": "c",
            "price": 0, "updatedAt": "2026-09-01T00:00:00Z"}
    assert ProductRecord(**{**base, "available": True}).availability == "available"
    assert ProductRecord(**{**base, "available": False}).availability == "unavailable"
    assert ProductRecord(**base).availability == "unknown"                                # 필드 없음
    assert ProductRecord(**{**base, "available": None}).availability == "unknown"         # 명시적 null
    assert ProductRecord(**{**base, "availability": "unknown"}).availability == "unknown"  # 3값을 그대로 줘도 받는다
    with pytest.raises(ValidationError):
        ProductRecord(**{**base, "availability": "품절"})                                  # 3값 밖은 거부
