"""내부 자료형 — 파이프라인 단계 사이를 오가는 값. 외부 계약(schemas.py)과 독립.

이름 규칙(팀 결정 2026-09-21): Python 내부는 **snake_case**, HTTP 요청·응답(schemas.py)만 **camelCase**.
두 세계의 변환은 pipeline.to_internal()(7.6 DTO → ProfileRequest)과 backend.to_callback()(ProfileOutcome → 7.7 DTO) 두 곳뿐.

근거 문서: 4단계 2.2(흐름·의사코드), 5단계 1.2(프로필 테이블)·3.2(모델 입력), 6단계 1.2·3.2.
리뷰 신호(별점 → 카테고리 편향 · 리뷰 행 보관 — 원문은 빼고 해시만, 10-11 결정 B)는 2026-10-07 결정 — 규칙표_별점x글 §3·§4(팀 폴더 일일작업물/첨부/2026-10-07_리뷰_신호_처리).
실험 산출물(`extract_3axis.json`, `profiles_3axis_v0.json`)을 그대로 읽을 수 있도록 LLM 출력·판정 로그의 키 이름은
실험과 같게 두었다(camelCase 키는 alias로 받는다). 판정 문자열 상수는 실험과 동일 — 바꾸면 회귀 비교가 안 된다.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from profiling import guard

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
    preferred_tags: list[str] = Field(description="검증기 통과 태그 글자(keep 순서) — 메모리에서만 쓴다. DB 의 preferred_tags 는 축 · 출처가 붙은 "
                                                  "모양(RecipientProfile.preferred_tags · profile_tags)")
    disliked_tags: list[str] = Field(description="Backend 비선호 카테고리 이름 — 메모리에서만(DB 엔 disliked_categories 로, DR-040)")
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
    tag_status: str | None = None       # 태그 단계 결과 — ok · empty · bad_json · fail:<종류> · skip_no_signal · off · skip
    llm_cost_usd: float | None = None   # 외부 API 실제 청구액(OpenRouter usage.cost). 로컬 · 끔이면 None
    updated_at: datetime | None = None  # **읽을 때만** 채운다 (store.get_run). save()는 무시하고 DB가 now()로 쓴다.
                                        # 접수 단계 중복 판정이 "이 RUNNING이 아직 살아 있나"를 이 값으로 본다

# ---------------------------------------------------------------------------
# 리뷰 신호 — 별점 → 카테고리 편향, 리뷰 한 건의 보관 형 (규칙표_별점x글 §3·§4, 2026-10-07 결정)
#   별점은 "그 카테고리를 얼마나 좋아하나"의 지표(카테고리 층). 글은 상세 태그(태그 층 — LLM, 하네스 뒤에 붙는다).
#   점수 = 별점 − 3 (−2~+2) · 카테고리 편향 = 내가 매긴 상품들의 점수 합 ÷ (n + 2). "+2"가 0점 두 개로 시작(가상 3점 2개).
#   편향은 풀의 **순서만** 조정한다. 리뷰 없는 카테고리 = 0(중립). 1~2점 상품도 제외하지 않는다.
#   직접 선택한 비선호 카테고리는 v1 제외 · v2 감점(10-11 결정, pipeline.DislikeMode — 제외 규칙은 스위치로 남김).
# ---------------------------------------------------------------------------

NEUTRAL_RATING = 3     # 이 별점이 0점. 1·2점은 음수, 4·5점은 양수
PRIOR_REVIEWS = 2      # 편향 분모에 더하는 "0점 리뷰" 수. 리뷰 1건이 곧바로 ±2가 되지 않게 한다 (1점 하나 → −2 ÷ 3 = −0.67)

_WS = re.compile(r"\s+")


def rating_score(rating: int) -> int:
    """별점 1~5 → 점수 −2~+2. 3점이 0."""
    return rating - NEUTRAL_RATING


class CategoryScore(BaseModel):
    """소분류 하나의 누적 — recipient_profiles.category_scores 의 원소. 편향은 저장하지 않고 읽을 때 계산한다."""

    category_id: int
    n: int = Field(ge=0, description="내가 별점을 매긴 상품 수 (같은 상품은 최신 1건)")
    score_sum: int = Field(description="점수(별점 − 3)의 합")

    @property
    def bias(self) -> float:
        """점수 합 ÷ (n + 2). 범위 −2~+2, 0이 중립. n = 0이면 0."""
        return self.score_sum / (self.n + PRIOR_REVIEWS)


def score_categories(reviews: list[Review], category_by_product: Mapping[int, int]) -> list[CategoryScore]:
    """리뷰 목록 → 소분류별 누적(CategoryScore). category_by_product 는 상품 번호 → 소분류 번호 (pipeline 이 카탈로그 by_id 로 만든다).

    규칙: 카탈로그에 없는 상품은 건너뛴다 · 같은 상품이 두 번 오면 앞의 것만(BE 가 작성일 최신순으로 보낸다) ·
    결과는 category_id 오름차순(같은 입력이면 같은 JSON). 시험: tests/unit/test_recipient_profile.py::test_score_categories_*
    """
    acc: dict[int, CategoryScore] = {}
    seen: set[int] = set()
    for r in reviews:
        if r.product_id in seen:
            continue
        seen.add(r.product_id)
        category_id = category_by_product.get(r.product_id)
        if category_id is None:
            continue
        c = acc.setdefault(category_id, CategoryScore(category_id=category_id, n=0, score_sum=0))
        c.n += 1
        c.score_sum += rating_score(r.rating)
    return [acc[k] for k in sorted(acc)]


def normalize_text(text: str | None) -> str:
    """글 없음(None)·빈 글·공백만은 모두 "" — 7.6 은 글이 없으면 null 로 보내야 하지만 "" 나 공백으로 와도 같게 본다. 안쪽 공백은 한 칸으로."""
    return _WS.sub(" ", (text or "").strip())


def review_hash(review: Review) -> str:
    """리뷰 한 건의 캐시 키 — sha256(product_id · rating · 정규화한 글), 64자. 저장된 행과 같으면 글을 다시 읽을(LLM) 필요가 없다.

    별점을 넣는 이유: 별점이 바뀐 리뷰는 "다른 리뷰"다(10-07 결정 — 리뷰 단위 hash(productId, rating, 정규화 글)).
    """
    raw = f"{review.product_id}\n{review.rating}\n{normalize_text(review.review_text)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def preference_hash(text: str | None) -> str | None:
    """취향 문장의 지문 — sha256(정규화한 글), 64자. 없거나 공백만이면 None. 원문 대신 이것만 저장한다(10-11 결정 B)."""
    t = normalize_text(text)
    return hashlib.sha256(t.encode("utf-8")).hexdigest() if t else None


class ReviewRecord(BaseModel):
    """ai_profile.recipient_reviews 한 행 — 수신자 한 사람의 리뷰 ≤10, 상품당 최신 1건. 분석마다 행 전체를 교체한다(스냅샷).

    **리뷰 글은 가드레일 정리본만 저장한다**(10-13 DR-039 — 결정 C 의 구현). 정리본 = guard.clean(개인정보 · 욕설 가림, 의미 없는 글 ·
    인젝션 의심 글은 None). 원문 그대로는 저장하지 않는다 — 원문 정본은 Backend. review_hash 는 원문 기준(바뀜 판정 · 캐시 키).
    """

    product_id: int
    rating: int = Field(ge=1, le=5)
    review_hash: str = Field(min_length=64, max_length=64, description="review_hash() — 캐시 키 (상품·별점·정규화 글)")
    position: int = Field(ge=0, le=9, description="7.6 reviews 배열에서의 자리(0이 가장 최신, 7.6 상한 10개 → 0~9 · DB CHECK 와 같음). "
                                                   "건너뛴 중복이 있으면 번호가 빌 수 있다")
    text_clean: str | None = Field(default=None, description="리뷰 글 정리본(guard.clean) — 글 없음 · 의미 없음 · 인젝션 의심이면 None")


def to_review_records(reviews: list[Review]) -> list[ReviewRecord]:
    """7.6 리뷰 목록 → 보관 행. 같은 상품이 두 번 오면 앞의 것(최신)만 — BE 가 걸러 보내지만 PK(수신자, 상품) 위반을 코드에서 먼저 막는다."""
    out: list[ReviewRecord] = []
    seen: set[int] = set()
    for pos, r in enumerate(reviews):
        if r.product_id in seen:
            continue
        seen.add(r.product_id)
        out.append(ReviewRecord(product_id=r.product_id, rating=r.rating, review_hash=review_hash(r), position=pos,
                                text_clean=guard.clean(r.review_text).text))
    return out


# ---------------------------------------------------------------------------
# 수신자 프로필 — ai_profile.recipient_profiles 한 행과 그 갱신 규칙
# (담당파트 상세설계서 §1.7: 태그는 AI가 보관, 한 사람당 한 행, 최신 분석이 덮어씀)
# 열 구성과 v3 계획은 docs/코드_안내서.md
# ---------------------------------------------------------------------------

class ProfileTag(BaseModel):
    """검증기 통과 선호 태그 하나 — recipient_profiles.preferred_tags 의 원소. 챗봇 조회 API 가 그대로 내준다."""

    axis: str = Field(description="태그 규격의 축 — tag3: 카테고리 · IP · 브랜드(tag4 는 + 특징)")
    tag: str
    source: Literal["preference", "review_text"] = Field(description="근거가 취향 문장인지 리뷰인지")
    ref: str | None = Field(default=None, description="근거 자리 — pref · r1 · r2 … (여러 개면 'pref, r1')")


class RecipientProfile(BaseModel):
    """ai_profile.recipient_profiles 한 행."""

    recipient_user_id: int
    source_version: int
    profile_run_id: str | None = None
    preferred_tags: list[ProfileTag] = Field(default_factory=list,
                                             description="선호 태그(축 · 출처 · 근거 자리) — 추천 30개를 고를 때 쓴 태그 그대로, 자르지 않는다. "
                                                         "ValidationResult.preferred_tags(글자 목록, 메모리)와 이름만 같다")
    axes: dict[str, list[ValidatedTag]] = Field(default_factory=lambda: {"likes": [], "key_features": [], "dislikes": []})
    disliked_categories: list[DislikedCategory] = Field(default_factory=list)   # 7.6 의 명시 비선호(번호 + 이름) 사본 — 비선호는 이것 하나(DR-040)
    recommended_product_ids: list[int] = Field(default_factory=list, max_length=30)
    gift_preference_hash: str | None = Field(default=None, min_length=64, max_length=64,
                                             description="7.6 giftPreference 원문의 지문(preference_hash) — 바뀜 판정용. 없거나 공백이면 None")
    gift_preference_clean: str | None = Field(default=None, description="취향 문장 정리본(guard.clean) — 원문은 저장하지 않음(DR-039). "
                                                                        "없거나 의미 없음 · 인젝션 의심이면 None")
    guard_version: str | None = Field(default=None, description="정리 규칙 판(guard.GUARD_VERSION)")
    tag_status: str | None = Field(default=None, description="태그 단계 결과 — ProfileOutcome.tag_status")
    category_scores: list[CategoryScore] = Field(default_factory=list, description="소분류별 별점 누적 — 편향 = score_sum ÷ (n + 2)")
    reviews: list[ReviewRecord] = Field(default_factory=list, max_length=10, description="이번 분석에 쓴 리뷰 ≤10 — recipient_reviews 자식 행, 스냅샷 교체")
    catalog_version_id: UUID | None = None
    prompt_version: str | None = None
    validator_version: str | None = None
    updated_at: datetime | None = None


def from_outcome(rq: ProfileRequest, outcome: ProfileOutcome, profile_run_id: str | None = None, *,
                 category_scores: list[CategoryScore] | None = None) -> RecipientProfile:
    """ProfileOutcome(+요청) → RecipientProfile 한 행. pipeline.profile()이 RESULT_READY 저장 직후 부른다.

    - preferred_tags: 판정 로그의 keep 결정에서 축(axis4) · 출처(ref)를 붙여(profile_tags) — 추천 30개를 고를 때 쓴 태그 그대로, 자르지 않는다
      (DR-040 — 12개로 자르던 때는 저장된 태그와 추천에 쓴 태그가 달랐다). 챗봇이 축별로 읽는다. tag_status 는 태그 단계 결과(실패면 태그는 [] ·
      상태로 구분 — 옛 태그를 남기지 않는다: 입력이 바뀐 새 번호에 옛 태그가 붙어 보이지 않게).
    - axes: ValidationResult의 3축 원형(v1은 전부 []).
    - disliked_categories: 요청(rq)에서 그대로 — "분석 시점 사본"이라 Backend가 이후 바꿔도 다음 7.6까지 모른다(설계서 §1.7).
      이름만 옮긴 disliked_tags 는 두지 않는다(DR-040).
    - recommended_product_ids · catalog_version_id: outcome.search에서(콜백과 같은 30개).
    - gift_preference_hash · reviews: 요청(rq)에서. 원문 그대로는 남기지 않는다 — 취향 문장 · 리뷰 글은 **가드레일 정리본**만
      (gift_preference_clean · ReviewRecord.text_clean, 10-13 DR-039), 지문(해시)은 원문 기준. 리뷰는 to_review_records()로 행이 된다.
    - category_scores: 호출자가 넘긴다(score_categories — 카탈로그가 필요해 pipeline이 만든다). 안 넘기면 [].
    """
    v = outcome.validation
    s = outcome.search
    return RecipientProfile(
        recipient_user_id=rq.recipient_user_id,
        source_version=rq.source_version,
        profile_run_id=profile_run_id,
        preferred_tags=profile_tags(v),
        axes={"likes": list(v.likes), "key_features": list(v.key_features), "dislikes": list(v.dislikes)} if v
             else {"likes": [], "key_features": [], "dislikes": []},
        disliked_categories=list(rq.disliked_categories),
        recommended_product_ids=list(s.product_ids) if s else [],
        gift_preference_hash=preference_hash(rq.gift_preference),
        gift_preference_clean=guard.clean(rq.gift_preference).text,
        guard_version=guard.GUARD_VERSION,
        tag_status=outcome.tag_status,
        category_scores=list(category_scores or []),
        reviews=to_review_records(rq.reviews),
        catalog_version_id=s.catalog_version_id if s else None,
        prompt_version=outcome.prompt_version,
        validator_version=outcome.validator_version,
    )


def profile_tags(validation: ValidationResult | None) -> list[ProfileTag]:
    """판정 로그의 keep → ProfileTag(축 · 글자 · 출처 · 근거 자리). 축이 없는 옛 결과는 likes = 카테고리 · key_features = 특징으로 본다."""
    if validation is None:
        return []
    out: list[ProfileTag] = []
    for d in validation.log:
        if d.decision != "keep":
            continue
        axis = getattr(d, "axis4", None) or ("특징" if d.axis == "key_features" else "카테고리")
        ref = d.ref or None
        out.append(ProfileTag(axis=axis, tag=d.tag, source="preference" if ref and "pref" in ref else "review_text", ref=ref))
    return out


def should_replace(current: RecipientProfile | None, incoming: RecipientProfile) -> bool:
    """기존 행을 새 결과로 덮어쓸지 — source_version이 같거나 높을 때만. 낮으면 순서 역전(늦게 도착한 옛 분석) → 기존 유지.

    DB adapter의 upsert도 같은 규칙을 SQL(WHERE source_version <= excluded.source_version)로 한 번 더 건다 — 두 프로세스가
    동시에 써도 DB가 최종 심판. 이 함수는 호출자의 사전 판단용(테스트 가짜 저장소도 이 규칙을 따른다).
    """
    return current is None or current.source_version <= incoming.source_version


def to_search_hint(profile: RecipientProfile) -> dict:
    """Chat/Search가 쓰는 형태로 변환 — v3에서 Search 인자와 맞춘다."""
    raise NotImplementedError
