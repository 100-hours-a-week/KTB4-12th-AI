"""내부 자료형 — 파이프라인 단계 사이를 오가는 값. 외부 계약(schemas.py)과 독립.

이름 규칙(팀 결정 2026-09-21): Python 내부는 **snake_case**, HTTP 요청·응답(schemas.py)만 **camelCase**.
두 세계의 변환은 pipeline.to_internal()(7.6 DTO → ProfileRequest)과 backend.to_callback()(ProfileOutcome → 7.7 DTO) 두 곳뿐.

근거 문서: 4단계 2.2(흐름·의사코드), 5단계 1.2(프로필 테이블)·3.2(모델 입력), 6단계 1.2·3.2.
실험 산출물(`extract_3axis.json`, `profiles_3axis_v0.json`)을 그대로 읽을 수 있도록 LLM 출력·판정 로그의 키 이름은
실험과 같게 두었다(camelCase 키는 alias로 받는다). 판정 문자열 상수는 실험과 동일 — 바꾸면 회귀 비교가 안 된다.
"""

from __future__ import annotations

from datetime import datetime
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


class ErrorCode(StrEnum):
    """AI 내부 오류 분류 — 실행 기록(profile_runs.error.code)과 로그에 남긴다.

    HTTP 상태 코드는 계약(문서 1: 400 INVALID_REQUEST 등)대로 두고, "무엇이 안 맞았는지"는 이 코드로 구분한다.
    CONTRACT_* 는 Backend와의 필드 계약 불일치 — 연동 초기에 가장 흔한 사고라 따로 이름을 둔다.
    """

    CONTRACT_7_6_UNKNOWN_FIELD = "CONTRACT_7_6_UNKNOWN_FIELD"   # BE→AI 7.6 본문에 계약에 없는 필드. 무시하고 경고만(400 아님)
    CONTRACT_7_7_REJECTED = "CONTRACT_7_7_REJECTED"             # AI→BE 7.7이 4xx(400·401·403)로 거부됨. 본문·토큰 계약 불일치 의심. 재시도 없음
    CONTRACT_7_9_SCHEMA = "CONTRACT_7_9_SCHEMA"                 # BE→AI 7.9 export 필드 불일치(필수 누락·타입). 가져오기 CLI가 저장을 거부
    CALLBACK_STALE = "CALLBACK_STALE"                           # 7.7 409 — Backend에 더 새 버전이 있어 폐기(SUPERSEDED). 정상 경로
    CALLBACK_UNREACHABLE = "CALLBACK_UNREACHABLE"               # 7.7 5xx·타임아웃·연결 실패 — 미전달, 재전송 대상(RESULT_READY 유지)
    NO_ACTIVE_CATALOG = "NO_ACTIVE_CATALOG"                     # 활성 카탈로그 없음
    STORE_FAILED = "STORE_FAILED"                               # 실행 기록·프로필 저장 실패(DB)
    PIPELINE_ERROR = "PIPELINE_ERROR"                           # 그 밖의 처리 중 예외


class CallbackResult(BaseModel):
    """BackendPort.send_profile_callback()의 결과 — 실행 기록의 다음 상태 + 왜 그런지."""

    status: RunStatus
    http_status: int | None = None
    code: ErrorCode | None = None          # DELIVERED면 None
    message: str | None = None             # Backend 응답의 error.code·message 등 사람이 읽을 한 줄


class ProfileOutcome(BaseModel):
    """`profile()` 한 건의 결과 — 저장·콜백의 입력. 실행 기록(ai_profile.profile_runs) 한 행과 1:1.

    status가 바뀔 때마다 같은 객체를 model_copy(update=…)로 복사해 store.save()에 넘긴다:
      RUNNING(접수) → RESULT_READY(결과) → DELIVERED | SUPERSEDED | FAILED(콜백 뒤)  — 3단계 구현 상세 §10.2
    """

    recipient_user_id: int
    source_version: int
    status: RunStatus
    input_hash: str | None = None       # 정규화한 7.6 본문 해시 (pipeline.input_hash). DB 저장 시 필수 — 같은 키·다른 입력 감지용
    validation: ValidationResult | None = None
    search: SearchResult | None = None
    failure_code: ErrorCode | None = None   # 실패·미전달의 분류 (profile_runs.error.code)
    failure_reason: str | None = None       # 사람이 읽을 사유 (profile_runs.error.reason)
    callback_attempts: int = 0          # 7.7 시도 횟수. run_and_callback이 콜백 뒤 +1 해서 저장
    prompt_version: str | None = None
    validator_version: str | None = None

# ---------------------------------------------------------------------------
# 수신자 프로필 — ai_profile.recipient_profiles 한 행과 그 갱신 규칙
# (담당파트 상세설계서 §1.7: 태그는 AI가 보관, 한 사람당 한 행, 최신 분석이 덮어씀)
# 열 구성과 v3 계획은 docs/코드_안내서.md
# ---------------------------------------------------------------------------

MAX_PREFERRED_TAGS = 12
MAX_DISLIKED_TAGS = 8


class RecipientProfile(BaseModel):
    """ai_profile.recipient_profiles 한 행."""

    recipient_user_id: int
    source_version: int
    profile_run_id: str | None = None
    preferred_tags: list[str] = Field(default_factory=list, max_length=MAX_PREFERRED_TAGS)
    disliked_tags: list[str] = Field(default_factory=list, max_length=MAX_DISLIKED_TAGS)
    axes: dict[str, list[ValidatedTag]] = Field(default_factory=lambda: {"likes": [], "key_features": [], "dislikes": []})
    disliked_categories: list[DislikedCategory] = Field(default_factory=list)   # 7.6에서 받은 명시 비선호 — 분석 시점 사본 (테이블 열과 같은 이름)
    recommended_product_ids: list[int] = Field(default_factory=list, max_length=30)
    catalog_version_id: UUID | None = None
    prompt_version: str | None = None
    validator_version: str | None = None
    updated_at: datetime | None = None


def from_outcome(rq: ProfileRequest, outcome: ProfileOutcome, profile_run_id: str | None = None) -> RecipientProfile:
    """ProfileOutcome(+요청) → RecipientProfile 한 행. pipeline.profile()이 RESULT_READY 저장 직후 부른다.

    - preferred/disliked 태그: outcome.validation의 2필드를 상한(12·8)으로 자른다. v1은 disliked = 비선호 카테고리 이름뿐.
    - axes: ValidationResult의 3축 원형(v1은 전부 []).
    - disliked_categories: 요청(rq)에서 그대로 — "분석 시점 사본"이라 Backend가 이후 바꿔도 다음 7.6까지 모른다(설계서 §1.7).
    - recommended_product_ids · catalog_version_id: outcome.search에서(콜백과 같은 30개).
    """
    v = outcome.validation
    s = outcome.search
    return RecipientProfile(
        recipient_user_id=rq.recipient_user_id,
        source_version=rq.source_version,
        profile_run_id=profile_run_id,
        preferred_tags=cap_tags(v.preferred_tags, MAX_PREFERRED_TAGS) if v else [],
        disliked_tags=cap_tags(v.disliked_tags, MAX_DISLIKED_TAGS) if v else [],
        axes={"likes": list(v.likes), "key_features": list(v.key_features), "dislikes": list(v.dislikes)} if v
             else {"likes": [], "key_features": [], "dislikes": []},
        disliked_categories=list(rq.disliked_categories),
        recommended_product_ids=list(s.product_ids) if s else [],
        catalog_version_id=s.catalog_version_id if s else None,
        prompt_version=outcome.prompt_version,
        validator_version=outcome.validator_version,
    )


def should_replace(current: RecipientProfile | None, incoming: RecipientProfile) -> bool:
    """기존 행을 새 결과로 덮어쓸지 — source_version이 같거나 높을 때만. 낮으면 순서 역전(늦게 도착한 옛 분석) → 기존 유지.

    DB adapter의 upsert도 같은 규칙을 SQL(WHERE source_version <= excluded.source_version)로 한 번 더 건다 — 두 프로세스가
    동시에 써도 DB가 최종 심판. 이 함수는 호출자의 사전 판단용(테스트 가짜 저장소도 이 규칙을 따른다).
    """
    return current is None or current.source_version <= incoming.source_version


def merge_explicit_dislikes(profile: RecipientProfile, category_names: list[str]) -> RecipientProfile:
    """Backend 명시 비선호를 disliked_tags 앞에 병합 (중복 제거·상한)."""
    raise NotImplementedError


def cap_tags(tags: list[str], limit: int) -> list[str]:
    """중복 제거(앞의 것을 남김 — 검증기가 확정 우선으로 정렬해 두었으므로) 후 앞에서 limit개. 빈 문자열·공백은 버린다."""
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        t = tag.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]


def to_search_hint(profile: RecipientProfile) -> dict:
    """Chat/Search가 쓰는 형태로 변환 — v3에서 Search 인자와 맞춘다."""
    raise NotImplementedError
