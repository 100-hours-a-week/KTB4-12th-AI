"""내부 자료형 — 파이프라인 단계 사이를 오가는 값. 외부 계약(transport/schemas.py)과 독립.

이름 규칙(팀 결정 2026-09-21): Python 내부는 **snake_case**, HTTP 요청·응답(transport/schemas.py)만 **camelCase**.
두 세계의 변환은 pipeline.to_internal()(7.6 DTO → ProfileRequest)과 adapters.backend.to_callback()(ProfileOutcome → 7.7 DTO) 두 곳뿐.

근거 문서: 4단계 2.2(흐름·의사코드), 5단계 1.2(프로필 테이블)·3.2(모델 입력), 6단계 1.2·3.2.
실험 산출물(`extract_3axis.json`, `profiles_3axis_v0.json`)을 그대로 읽을 수 있도록 LLM 출력·판정 로그의 키 이름은
실험과 같게 두었다(camelCase 키는 alias로 받는다). 판정 문자열 상수는 실험과 동일 — 바꾸면 회귀 비교가 안 된다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 7.6 요청의 내부 형 — api/에서 ProfileExtractRequest → ProfileRequest로 변환
# ---------------------------------------------------------------------------


class DislikedCategory(BaseModel):
    category_id: int
    category_name: str


class Review(BaseModel):
    product_id: int
    rating: int
    review_text: str | None = None


class ProfileRequest(BaseModel):
    """프로파일링 한 건의 입력. 실행 기록·검증기·검색이 모두 이것을 본다."""

    recipient_user_id: int
    source_version: int
    gift_preference: str | None
    disliked_categories: list[DislikedCategory]
    reviews: list[Review]


# ---------------------------------------------------------------------------
# 카탈로그 조인 결과 — LLM 입력 (5단계 3.2)
# ---------------------------------------------------------------------------


class ReviewWithProduct(BaseModel):
    """리뷰에 활성 카탈로그의 상품 정보를 붙인 것. 모델에는 `ref`(r1~rN)만 보이고 `product_id`는 보이지 않는다."""

    ref: str = Field(description="r1, r2, … — 프롬프트에서 리뷰를 가리키는 번호")
    product_id: int
    rating: int
    review_text: str | None
    product_found: bool = Field(description="False면 카탈로그에 없는 상품 — 원문·별점만 쓰고 카테고리 수준 출처만 허용")
    name: str | None = None
    brand: str | None = None
    category_id: int | None = None
    category_name: str | None = None
    description: str | None = None
    kind: str | None = Field(default=None, description="품목명(설명의 '○○ 유형'). export에 없으면 None")


# ---------------------------------------------------------------------------
# LLM 출력 — JSON 스키마 그대로 (3축, 항목별 근거)
# ---------------------------------------------------------------------------

EvidenceSource = Literal["preference", "review_text", "product_desc", "rating_only"]
"""근거 출처. preference=취향 문장(+명시 비선호 목록), review_text=해당 리뷰 글, product_desc=해당 상품 설명,
rating_only=글 없는 리뷰 — 카테고리·상품명 수준만 허용."""


class DraftItem(BaseModel):
    """모델이 낸 태그 한 개. 실험 출력 키(`productId`)를 alias로 받는다."""

    model_config = ConfigDict(populate_by_name=True)

    tag: str
    source: EvidenceSource
    evidence: str | None = None
    ref: str | None = Field(default=None, description="r1~rN 또는 '-'(취향 문장)")
    product_id: int | None = Field(default=None, alias="productId")


class ExtractDraft(BaseModel):
    """모델 1회 호출의 JSON 출력. 종류=likes, 특징=key_features, 싫어함=dislikes."""

    likes: list[DraftItem] = []
    dislikes: list[DraftItem] = []
    key_features: list[DraftItem] = []


# ---------------------------------------------------------------------------
# 검증기 출력 — 확정 태그 + 판정 로그
# ---------------------------------------------------------------------------

Axis = Literal["likes", "dislikes", "key_features"]

CheckResult = Literal[
    "evidence_ok",  # 근거 문장이 출처 원문에 있음
    "tag_in_source",  # 근거는 틀렸지만 태그 자체가 원문에 있음
    "hallucinated",  # 둘 다 없음 → 폐기
    "empty_tag",
    "review_text_null",  # 글 없는 리뷰를 글 출처로 인용
    "no_source_text",  # 설명 null · 상품 못 찾음
]

DecisionKind = Literal[
    "keep",
    "drop",
    "drop_product_type_from_desc",
    "drop_conflict_explicit_dislike",
    "duplicate_explicit_dislike",
    "candidate",
    "promote",
    "candidate_not_promoted",
]

Grade = Literal["confirmed", "promoted"]
PromotedBy = Literal["min_products", "single_pos_exact"]


class ValidatedTag(BaseModel):
    """검증을 통과해 프로필에 남는 태그 한 개."""

    tag: str
    source: EvidenceSource
    grade: Grade
    products: list[int] = Field(default_factory=list, description="promoted일 때 근거가 된 상품 ID")
    promoted_by: PromotedBy | None = None


class Decision(BaseModel):
    """판정 로그 한 줄 — 모델이 낸 항목마다 하나. 실험 `log` 항목과 같은 키."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    tag: str
    axis: Axis
    decision: DecisionKind
    check: CheckResult | None = None
    source: EvidenceSource | None = None
    evidence: str | None = None
    ref: str | None = None
    product_id: int | None = Field(default=None, alias="productId")
    moved: str | None = None
    canonicalized_from: str | None = None
    products: int | None = Field(default=None, description="candidate·promote일 때 서로 다른 상품 수")
    promoted_by: PromotedBy | None = None


class ValidationResult(BaseModel):
    """검증기 출력. 3축(내부 보관)과 2필드(선호·비선호 태그), 판정 로그."""

    likes: list[ValidatedTag]
    key_features: list[ValidatedTag]
    dislikes: list[ValidatedTag]
    preferred_tags: list[str] = Field(description="likes + key_features 병합, 확정 우선, 상한 max_preferred_tags")
    disliked_tags: list[str] = Field(description="Backend 비선호 카테고리명이 앞, 상한 max_disliked_tags")
    log: list[Decision]


# ---------------------------------------------------------------------------
# 검색 결과 · 실행 기록
# ---------------------------------------------------------------------------


class SearchResult(BaseModel):
    """Search가 돌려주는 것. 상품 ID 순서가 곧 추천 순위."""

    product_ids: list[int] = Field(max_length=30)
    query_text: str
    catalog_version_id: UUID          # ai_search.catalog_versions.id (팀원 DB) · 파일 카탈로그는 고정값


class RunStatus(StrEnum):
    """실행 기록 상태 (3단계 구현 상세 §10.2)."""

    RUNNING = "RUNNING"
    RESULT_READY = "RESULT_READY"
    DELIVERED = "DELIVERED"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


class ProfileOutcome(BaseModel):
    """`profile()` 한 건의 결과 — 저장·콜백의 입력."""

    recipient_user_id: int
    source_version: int
    status: RunStatus
    validation: ValidationResult | None = None
    search: SearchResult | None = None
    failure_reason: str | None = None
    prompt_version: str | None = None
    validator_version: str | None = None
