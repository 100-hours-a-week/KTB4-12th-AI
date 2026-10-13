"""파이프라인 — 7.6 요청 한 건을 끝까지 처리한다. 4단계 2.2 `profile()` 의사코드의 자리 (Application Service).

이 파일은 "무엇을 어떤 순서로"만 안다. 바깥(파일·DB·HTTP)은 ports의 모양으로만 받고(인자 catalog·store), 구현 모듈을 import하지 않는다.
그래서 단위 테스트는 ports 모양의 가짜(dict 하나짜리 클래스)를 넣어 DB 없이 돈다.

v1: 취향 문장 None · 리뷰 [] → 추출·검증을 건너뛰고, 비선호 카테고리를 뺀(또는 감점한) 상품 풀 30개를 만든다.
v2(지금): 리뷰 별점 → 소분류 편향(score_categories)을 풀 순서에 더하고, 30개는 D(selection — 비선호 하드 제외 · 한 소분류 최대 10 ·
    같은 모델 하나)로 고른다. 글·취향 → 태그(LLM)는 아직 없음 — 태그가 생기면 D 의 임베딩 단계가 켜진다.
입력 세 칸이 전부 비면(비선호 0 · 취향 없음 · 리뷰 0) 추천 0개를 돌려준다 — has_no_input(), 10-09 결정 C.
v3: needs_model()이 True인 분기에 카탈로그 조인 → ProfileModel.analyze()(모델) → 검증기 → Search 호출이 들어온다. 나머지 흐름은 그대로.

흐름 (한 건):
  api.run_and_callback ──▶ profile(rq, catalog=, store=, recipient_store=)
                            0) store.save(RUNNING)         실행 기록 시작 — profile_runs 한 행 (input_hash 포함). 저장 못 하면 FAILED·콜백 없음
                            1) catalog.active()            활성 카탈로그 (없으면 NoActiveCatalog — api가 접수 단계에서 이미 걸렀지만 여기서도 FAILED로 기록)
                            2) validation 만들기           v1: 비선호 이름만 disliked_tags에, 나머지 빈 값
                               category_scores            리뷰 별점 → 소분류 편향 (1)에서 받은 products 로 소분류를 찾는다 — 같은 카탈로그 버전)
                            3) make_pool()                 rank_products()(인기 + 편향 + 감점 순위) → 고르기: PoolStrategy.D(기본, selection.select_pool —
                                                           비선호 하드 제외 · 한 소분류 최대 10 · 같은 모델 하나 · 모자라면 비선호를 감점 순서로)
                                                           또는 S0(build_pool — 순위 앞 pool_size개, 10-12 까지의 규칙)
                            4) ProfileOutcome(RESULT_READY)
                            5) store.save(outcome)         실행 기록 갱신 — callback_payload·hash가 여기서 DB에 먼저 커밋된다 (§16.4)
                            6) recipient_store.upsert()    수신자 프로필 (recipient_profiles, category_scores 포함) — 낮은 버전이면 DB가 무시
                          ◀── outcome  (RESULT_READY면 api가 7.7 콜백, FAILED면 침묵)
"""

from __future__ import annotations

import hashlib
import logging
import time
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import NamedTuple
from uuid import UUID

from profiling import selection, tagging
from profiling.ports import (
    CatalogReader,
    EmbeddingIndex,
    NoActiveCatalog,
    ProfileRunStore,
    RecipientProfileStore,
    TagExtractError,
    TagExtractor,
    TagReply,
    TokenUsage,
)
from profiling.schemas import ProductRecord, ProfileExtractRequest
from profiling.types import (
    CategoryScore,
    DislikedCategory,
    ErrorCode,
    ProfileOutcome,
    ProfileRequest,
    Review,
    RunStatus,
    SearchResult,
    ValidationResult,
    from_outcome,
    score_categories,
)

log = logging.getLogger(__name__)

# 관측 라벨 — 어떤 규칙으로 만든 결과인지 저장해 두면 나중에 결과를 비교·회귀할 때 "언제 규칙이 바뀌었나"를 알 수 있다
VALIDATOR_VERSION_V1 = "v1-skip"      # 검증기 없이 비선호만 반영
VALIDATOR_VERSION_TAG4 = "tag4-v1"    # tagging.validate — 4축 · 근거 ref · 별점 4~5 · 판촉 단어 (10-12). 규격별 값은 TagSpec.validator_version
# 풀 정렬 규칙 라벨 (10-11 까지의 로그엔 "v1-view-count" = 조회수 내림차순만). 리뷰가 없어도 같은 라벨 — 라벨은 규칙이지 입력이 아니다
POOL_RULE_V2 = "v2-views+bias"                              # EXCLUDE: 인기(0~2) + BIAS_WEIGHT × 편향, 비선호는 뺌
POOL_RULE_V2_PENALTY = "v2-views+bias+dislike-penalty"      # PENALTY: 인기 + 편향 + (비선호면 DISLIKED_CATEGORY_SCORE)

# 리뷰 별점 편향(CategoryScore.bias, −2~+2)에 곱하는 가중치. 1.0 = 편향 +1 이 인기 절반(조회수 최대의 50%)과 같다.
# 값은 미결(결정_목록 3절 1번, 벤치로 정함 — 운영 중 LLM 이 정하지 않는다, 폐기 등록부 10-11). 리뷰 없는 소분류 = 편향 0
BIAS_WEIGHT = 1.0


class DislikeMode(StrEnum):
    """비선호 카테고리를 풀에서 어떻게 다루나 (10-11 결정).

    v1 은 EXCLUDE(하드 — 풀에서 뺀다), v2 부터 PENALTY(소프트 — 순서에서 감점, 설계 DR-005 · 위키 데이터-컨텍스트 §2.3).
    EXCLUDE 는 다시 쓸 수 있게 지우지 않고 남긴다. 고르는 곳은 설정 하나(settings.DISLIKE_MODE).
    PENALTY 를 쓰는 이유: 비선호를 잔뜩 넣는 입력(악성·장난)이 와도 풀이 비지 않는다 — 한 사람의 입력이 결과를 0개로 만들 수 없다.
    """

    EXCLUDE = "exclude"
    PENALTY = "penalty"


class PoolStrategy(StrEnum):
    """순위에서 30개를 어떻게 고르나 (10-12 결정 — docs/임베딩_추천_설계안_2026-10-12.md §6).

    D  비선호 하드 제외 · 한 소분류 최대 10 · 같은 모델 하나 · (태그 + 임베딩이 있으면) 임베딩 후보 + MMR. selection.select_pool
    S0 순위 앞 pool_size개 그대로(build_pool) — 10-12 까지의 규칙. 5점 소분류가 30개를 독차지하는 문제가 있어 D 로 바꿨고, 되돌릴 수 있게 남긴다.
    고르는 곳은 설정 하나(settings.POOL_STRATEGY).
    """

    D = "d"
    S0 = "s0"


# PENALTY 모드에서 **사용자가 직접 고른 비선호 카테고리의 기본 점수** — 이 상수에서 출발해 리뷰 편향을 **더한다**(10-11 결정).
# −2.0 = 편향 범위의 바닥. 리뷰가 없으면 조회수 최대 상품(인기 2.0)도 비선호면 0 — 조회수 0인 상품과 같은 자리, 사실상 맨 뒤지만 풀엔 남는다.
# 비선호 대분류 안의 상품에 5점을 줬으면 그만큼 올라간다(예: 5점 1건 → 편향 +0.67 → −1.33). 이 상수엔 BIAS_WEIGHT 를 곱하지 않고
# 편향 쪽만 곱한다(2단계: 점수 = 인기 + DISLIKED_CATEGORY_SCORE + BIAS_WEIGHT × 편향).
# 설계서 §2.3 의 0.2 는 코사인 유사도 눈금이라 여기 그대로 쓰지 않는다.
DISLIKED_CATEGORY_SCORE = -2.0


# ---------------------------------------------------------------------------
# 1) 변환 — HTTP 계약(camelCase) → 내부 자료형(snake_case). 두 세계가 만나는 곳은 여기와 backend.to_callback 뿐
# ---------------------------------------------------------------------------


def to_internal(body: ProfileExtractRequest) -> ProfileRequest:
    """7.6 DTO → ProfileRequest. 목록은 항목마다 내부 객체로 바꿔야 한다 — DTO 객체를 그대로 넣으면 pydantic이 거부한다."""
    return ProfileRequest(
        recipient_user_id=body.recipientUserId,
        source_version=body.sourceVersion,
        gift_preference=body.giftPreference,
        disliked_categories=[DislikedCategory(category_id=c.categoryId, category_name=c.categoryName) for c in body.dislikedCategories],
        reviews=[Review(product_id=r.productId, rating=r.rating, review_text=r.reviewText) for r in body.reviews],
    )


def input_hash(rq: ProfileRequest) -> str:
    """요청을 정규화해 sha256 — 실행 기록(profile_runs.input_hash)에 저장. 같은 (수신자, 버전)에 다른 입력이 오면 값이 달라진다(3단계 §10.2).

    정규화: 비선호 카테고리는 ID 순으로 정렬(Backend가 보내는 순서에 의존하지 않게). 리뷰는 받은 순서 그대로(최신순이 의미 있음).
    """
    canon = rq.model_copy(update={"disliked_categories": sorted(rq.disliked_categories, key=lambda c: c.category_id)})
    return hashlib.sha256(canon.model_dump_json().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 2) 판단 — 모델(LLM)이 필요한 요청인가 (결정 c)
# ---------------------------------------------------------------------------


def has_no_input(rq: ProfileRequest) -> bool:
    """입력 세 칸이 전부 비었나 — 비선호 0 · 취향 문장 없음(공백만도 없음) · 리뷰 0.

    10-09 결정(C): 이때 추천을 **0개**로 돌려준다. Backend 는 0행을 저장하고 상품 목록을 인기순으로 대체한다(빈 콜백은 BE 가 정상 완료로 처리).
    가입 직후에는 Backend 가 요청을 보내지 않으므로, 이 요청은 "기존 사용자가 전부 지웠다"는 신호다 — AI 사본(비선호 · 취향 · 리뷰)은 빈 값으로 덮인다.
    입력이 없는데 인기순 30개를 "AI 추천"으로 내보내던 것(v1)은 그만둔다 — 추천할 근거가 없으면 추천하지 않는다.
    """
    return not rq.disliked_categories and not (rq.gift_preference or "").strip() and not rq.reviews


def needs_model(rq: ProfileRequest) -> bool:
    """취향 문장이 있거나 리뷰가 하나라도 있으면 모델이 읽을 자료가 있는 것 → True. 둘 다 없으면(v1) False.

    v1에서는 True여도 모델 단계가 아직 없어 v1 경로로 처리하고 경고를 남긴다 (profile() 참고).
    """
    return rq.gift_preference is not None or bool(rq.reviews)


# ---------------------------------------------------------------------------
# 3) 풀 만들기 — v1의 "검색". 팀원 Search가 오면 이 함수 호출이 search.search(...)로 바뀐다 (결정 a)
# ---------------------------------------------------------------------------


def build_pool(rq: ProfileRequest, products: list[ProductRecord], pool_size: int, catalog_version_id: UUID, *,
               dislike_mode: DislikeMode = DislikeMode.EXCLUDE, scores: list[CategoryScore] | None = None) -> SearchResult:
    """상품을 **점수 내림차순**으로 pool_size개. 점수 = 인기 + 리뷰 별점 편향 (+ 비선호 감점). 비선호는 빼거나(EXCLUDE) 감점(PENALTY).

    점수 (두 모드 공통, v2 2단계 · 가이드 10-11 보강 A·D):
      인기   = **조회수 순위 백분위** × 2 (0~2) = 2 × (조회수가 나보다 낮은 후보 수) ÷ (후보 수 − 1). 1위 2.0 · 꼴찌 0 · 같은 조회수는 같은 값.
               후보는 **재고 필터만 거친 상품**(비선호를 빼기 전) — 두 모드에서 같은 상품의 인기가 같다.
               (10-12: 조회수 ÷ 최대 × 2 에서 바꿈. 순위는 조회수 분포와 상관없이 0~2 를 고르게 쓴다 — 실제 조회수는 소수 상품에
               몰리는 분포가 흔해, 비율이면 대부분이 0 근처로 눌린다. 지금 BE 조회수(임의값)는 고르게 퍼져 있어 두 방식 결과가 같다.
               주의: 상위 30개는 카탈로그의 0.7%라 편향 +0.67 이면 편향 소분류가 30개를 거의 다 차지한다 — 정규화가 아니라
               BIAS_WEIGHT 크기·소분류 상한 문제. docs/시나리오_검증_결과_2026-10-12.md)
      편향   = BIAS_WEIGHT × CategoryScore.bias (리뷰한 소분류만, 나머지 0). 편향 먼저·조회수 나중(사전식)이 아니라 같은 눈금으로 더한다 —
               사전식이면 5점 한 번 받은 소분류가 30개를 다 차지한다
      감점   = PENALTY 이고 직접 고른 비선호면 DISLIKED_CATEGORY_SCORE(−2.0). 편향은 그 위에 더한다(5점 받은 비선호 소분류는 덜 내려감)
    리뷰가 없으면 편향 0 → 인기순 = v1 결과 그대로(회귀 안전). 1~2점 소분류도 빼지 않는다(규칙표 §5) — 뒤로 갈 뿐.

    비선호는 **대분류**로 온다(09-27 BE 결정 — 사용자 친화성. BE `PreferenceSaveService`는 root 카테고리만 저장, 최대 5).
    그래서 상품의 대분류(`parentCategoryId`·`parentCategoryName`)가 비선호에 있으면 **그 하위 소분류 전체**가 비선호다.
    소분류 id·이름이 오는 경우도 그대로 비선호로 본다(파일 카탈로그·옛 시험과의 호환). 대분류 정보가 없는 상품(파일 카탈로그)은
    소분류 판정만 받는다.

    비선호 처리(dislike_mode, 10-11 결정):
      EXCLUDE  풀에서 뺀다 — v1 동작 그대로. 다시 쓸 수 있게 남겨 둔 하드 규칙
      PENALTY  빼지 않고 위 감점을 더한다. 비선호가 아닌 상품끼리의 순서는 EXCLUDE 와 같다 — 비선호가 없으면 결과도 같다

    재고(availability, 3값): **unavailable(재고 없음)만 뺀다.** unknown(재고 정보가 없는 상품)은 풀에 남긴다 — 09-23 Backend 합의.
    실제 재고를 아는 쪽은 Backend이므로 최종 판단을 Backend가 한다. 우리는 unknown을 available로 바꿔 쓰지 않고(검색기와 같은 규칙)
    "재고 없음으로 확인된 것만 제외"할 뿐이다. 패키지로 적재한 카탈로그는 전건 unknown이라, 이 규칙이 아니면 풀이 0건이 된다.

    정렬 기준(결정 a, 09-22 Backend 합의): 위 점수(조회수 기반) 내림차순. 동점은 productId 오름차순(카탈로그 순서)으로
    묶어 같은 입력이면 항상 같은 결과가 나오게 한다 — 임의성(random)은 넣지 않는다. Backend는 초기에 임의 조회수를 넣어 보내므로
    v1 배포 시점에도 정렬이 동작한다. 팀원 Search가 붙는 v3에서 이 함수 호출이 search.search(...)로 바뀐다.
    query_text는 v1에서 빈 문자열 — Search가 붙으면 검증기의 preferred_tags를 이어 붙인 질의가 들어간다.
    """
    ranked = rank_products(rq, products, dislike_mode=dislike_mode, scores=scores)
    seen = reviewed_ids(rq)                         # 리뷰한 상품(이미 받은 선물)은 추천하지 않는다(10-12)
    picked = [s.product.productId for s in ranked if s.product.productId not in seen][:pool_size]
    return SearchResult(product_ids=picked, query_text="", catalog_version_id=catalog_version_id)


def reviewed_ids(rq: ProfileRequest) -> frozenset[int]:
    """리뷰한 상품 = 이미 받은 선물 — 다시 추천하지 않는다(10-12 사용자 결정). 별점 · 태그는 취향 신호로만 쓴다."""
    return frozenset(r.product_id for r in rq.reviews)


def make_pool(rq: ProfileRequest, products: list[ProductRecord], pool_size: int, catalog_version_id: UUID, *,
              dislike_mode: DislikeMode = DislikeMode.EXCLUDE, scores: list[CategoryScore] | None = None,
              pool_strategy: PoolStrategy = PoolStrategy.D, tags: Sequence[selection.Tag] = (),
              embedding: EmbeddingIndex | None = None) -> tuple[SearchResult, str]:
    """풀 pool_size개와 규칙 라벨. 순위(rank_products)는 두 전략이 같고, 고르는 법만 다르다(PoolStrategy).

    두 전략 모두 **리뷰한 상품은 고르지 않는다**(reviewed_ids — 이미 받은 선물).
    D 에서 비선호 처리: EXCLUDE 면 순위에 비선호가 아예 없고, PENALTY 면 순위엔 있지만 **비선호 아닌 상품으로 다 못 채울 때만** 감점 순서로
    채운다 — 비선호 카테고리 상품에 높은 별점이 있어도 그 카테고리는 빼고, 그 상품의 태그만 취향 신호로 쓴다(selection 모듈 설명).
    """
    if pool_strategy is PoolStrategy.S0:
        rule = POOL_RULE_V2 if dislike_mode is DislikeMode.EXCLUDE else POOL_RULE_V2_PENALTY
        return build_pool(rq, products, pool_size, catalog_version_id, dislike_mode=dislike_mode, scores=scores), rule
    ranked = rank_products(rq, products, dislike_mode=dislike_mode, scores=scores)
    sel = selection.select_pool(ranked, pool_size, allow_disliked=dislike_mode is DislikeMode.PENALTY, tags=tags, embedding=embedding,
                                exclude_ids=reviewed_ids(rq))
    if sel.filled_disliked:
        log.info("profile recipient=%s 비선호 아닌 상품으로 %d개를 못 채워 비선호 %d개를 감점 순서로 채움",
                 rq.recipient_user_id, pool_size, sel.filled_disliked)
    rule = sel.rule + ("" if dislike_mode is DislikeMode.EXCLUDE else "+penalty-fill")
    return SearchResult(product_ids=sel.product_ids, query_text=sel.query_text, catalog_version_id=catalog_version_id), rule


class ScoredProduct(NamedTuple):
    """rank_products() 한 줄 — 점수가 어떻게 나왔는지까지(시나리오 검증 도구가 그대로 보여 준다). total = popularity + bias + penalty."""

    product: ProductRecord
    popularity: float      # 조회수 순위 백분위 × 2 (0~2)
    bias: float            # BIAS_WEIGHT × 소분류 편향 (리뷰 없는 소분류 0)
    penalty: float         # PENALTY 이고 직접 고른 비선호면 DISLIKED_CATEGORY_SCORE, 아니면 0
    total: float
    disliked: bool


def rank_products(rq: ProfileRequest, products: list[ProductRecord], *, dislike_mode: DislikeMode = DislikeMode.EXCLUDE,
                  scores: list[CategoryScore] | None = None) -> list[ScoredProduct]:
    """build_pool() 의 점수 계산 본체 — 후보 **전체**를 점수 내림차순(동점 productId 오름차순)으로, 점수 내역과 함께. 규칙은 build_pool 독스트링.

    EXCLUDE 면 비선호 상품은 목록에 없고, PENALTY 면 감점된 채 남는다. build_pool 은 이 목록의 앞 pool_size 개를 쓴다.
    """
    disliked_ids = {c.category_id for c in rq.disliked_categories}
    disliked_names = {c.category_name for c in rq.disliked_categories}

    def disliked(p: ProductRecord) -> bool:
        # ID가 정본, 이름은 보조 — Backend가 준 ID와 카탈로그 ID 체계가 어긋나는 사고(ID는 다른데 이름은 같음)까지 막는다.
        # 대분류(parent)가 걸리면 하위 소분류 전체가 빠진다.
        return (p.categoryId in disliked_ids or p.categoryName in disliked_names
                or (p.parentCategoryId is not None and p.parentCategoryId in disliked_ids)
                or (p.parentCategoryName is not None and p.parentCategoryName in disliked_names))

    candidates = [p for p in products if p.availability != "unavailable"]   # unknown은 남긴다 — 재고 판단은 Backend 몫
    pct = _view_percentile(candidates)                                       # 비선호를 빼기 전 기준
    bias_of = {s.category_id: s.bias for s in scores or []}
    penalize = dislike_mode is DislikeMode.PENALTY

    out: list[ScoredProduct] = []
    for p in candidates:
        is_disliked = disliked(p)
        if is_disliked and not penalize:
            continue
        popularity = 2.0 * pct(p.viewCount)
        bias = BIAS_WEIGHT * bias_of.get(p.categoryId, 0.0)
        penalty = DISLIKED_CATEGORY_SCORE if penalize and is_disliked else 0.0
        out.append(ScoredProduct(p, popularity, bias, penalty, popularity + penalty + bias, is_disliked))
    out.sort(key=lambda s: (-s.total, s.product.productId))
    return out


def _view_percentile(candidates: list[ProductRecord]) -> Callable[[int], float]:
    """조회수 → 순위 백분위(0~1) = (조회수가 더 낮은 후보 수) ÷ (후보 수 − 1). 같은 조회수는 같은 값, 후보가 1개 이하면 0."""
    views = sorted(p.viewCount for p in candidates)
    span = len(views) - 1
    if span <= 0:
        return lambda v: 0.0
    return lambda v: bisect_left(views, v) / span


def category_scores(rq: ProfileRequest, products: list[ProductRecord], catalog: CatalogReader) -> list[CategoryScore]:
    """리뷰 → 소분류별 별점 누적. 소분류는 **이미 받은 products**(profile 1)의 그 카탈로그 버전)에서 찾는다.

    catalog.by_id() 를 쓰지 않는 이유: DbCatalogReader.by_id() 는 부를 때마다 활성 버전을 따라잡아(catalog.py) 요청 도중 새 카탈로그가
    활성화되면 풀과 소분류 사전이 서로 다른 버전이 된다 — "한 요청 안에서 한 번만 잡아 끝까지 같은 버전" 원칙(가이드 10-11 보강 B).
    catalog 는 임시 번호 여부(provisional_ids)를 경고에 싣는 데만 쓴다.

    리뷰 상품이 카탈로그에 없으면 그 리뷰는 건너뛴다. BE 번호 회신(--id-map) 전에는 카탈로그가 수집처 숫자를 **임시 번호**로 써서
    BE 번호인 리뷰가 하나도 안 맞는다 — 오류 없이 편향만 비므로 원인까지 경고에 찍는다(보강 C). 원문은 남기지 않는다(번호·개수만).
    """
    reviewed = {r.product_id for r in rq.reviews}
    if not reviewed:
        return []
    category_of = {p.productId: p.categoryId for p in products}
    category_by_product = {pid: category_of[pid] for pid in reviewed if pid in category_of}
    if len(category_by_product) < len(reviewed):
        hint = " — 카탈로그가 임시 번호 사용 중(BE id-map 전)" if getattr(catalog, "provisional_ids", False) else ""
        log.warning("profile recipient=%s 리뷰 상품 %d/%d개만 카탈로그에 있음%s", rq.recipient_user_id,
                    len(category_by_product), len(reviewed), hint)
    return score_categories(rq.reviews, category_by_product)


# ---------------------------------------------------------------------------
# 4) 한 건 처리 — 접수(api)가 백그라운드에서 부른다
# ---------------------------------------------------------------------------


def profile(
    rq: ProfileRequest, *, catalog: CatalogReader, store: ProfileRunStore,
    recipient_store: RecipientProfileStore, pool_size: int = 30, dislike_mode: DislikeMode = DislikeMode.EXCLUDE,
    pool_strategy: PoolStrategy = PoolStrategy.D, embedding: EmbeddingIndex | None = None,
    tagger: TagExtractor | None = None, tag_spec: tagging.TagSpec | None = None,
) -> ProfileOutcome:
    """7.6 요청 한 건 → ProfileOutcome (저장까지). 예외를 밖으로 내지 않고 FAILED로 돌려준다.

    반환의 status가 RESULT_READY면 호출자(api.run_and_callback)가 7.7 콜백을 보내고, FAILED면 보내지 않는다(문서 1: AI는 침묵).
    catalog·store·recipient_store는 ports 모양이면 무엇이든 된다(파일/DB → 테스트 가짜).
    tag_spec 은 태그 규격(축 · 프롬프트) — 서비스는 기동 때 읽은 것을 넘기고, 없으면 tagging 의 기본 규격.
    """
    rid, sv = rq.recipient_user_id, rq.source_version
    h = input_hash(rq)

    # 0) 실행 기록 시작 — RUNNING 한 행. 여기서 실패하면(DB 없음) 아무것도 하지 않고 FAILED: 기록 없는 결과를 Backend에 보내지 않는다
    try:
        store.save(ProfileOutcome(recipient_user_id=rid, source_version=sv, status=RunStatus.RUNNING, input_hash=h,
                                  validator_version=VALIDATOR_VERSION_V1))
    except Exception as e:
        log.exception("profile recipient=%s source_version=%s 실행 기록(RUNNING) 저장 실패", rid, sv)
        return _fail(rq, store, ErrorCode.STORE_FAILED, f"저장 실패: {type(e).__name__}: {e}", save=False)

    # 1) 활성 카탈로그 — 한 요청 안에서 한 번만 잡아 끝까지 같은 버전을 쓴다 (4단계 snapshot 원칙)
    try:
        catalog_version_id, products = catalog.active()
    except NoActiveCatalog as e:
        return _fail(rq, store, ErrorCode.NO_ACTIVE_CATALOG, f"활성 카탈로그 없음: {e}")

    try:
        # 2) 리뷰 별점 → 소분류 편향 + (추출기가 있으면) 글 · 취향 → 4축 태그(LLM 한 번 → 코드 검증기)
        scores = category_scores(rq, products, catalog)
        disliked_names = [c.category_name for c in rq.disliked_categories]
        validation = ValidationResult(likes=[], key_features=[], dislikes=[], preferred_tags=[], disliked_tags=disliked_names, log=[])
        tags: list[selection.Tag] = []
        prompt_version, validator_version = None, VALIDATOR_VERSION_V1
        tag_status = "skip" if not needs_model(rq) else "off"   # 글 · 취향 없음 → skip, 추출기 꺼짐 → off
        llm_cost: float | None = None
        if needs_model(rq) and tagger is None:
            log.info("profile recipient=%s: 취향/리뷰(%d개) 있음 — 태그 추출기 꺼짐(TAGGER=none), 별점 편향 소분류 %d개만 반영",
                     rid, len(rq.reviews), len(scores))
        elif needs_model(rq):
            spec = tag_spec or tagging.SPEC
            tag_run = _extract_tags(rq, products, tagger, disliked_names, spec)
            validation, tags, prompt_version, tag_status = tag_run.validation, tag_run.tags, tag_run.prompt_version, tag_run.status
            llm_cost = tag_run.usage.cost_usd if tag_run.usage else None
            validator_version = spec.validator_version

        # 3) 풀 30개 — 입력이 전부 비면 추천 0개 (10-09 결정 C). 0개 콜백은 성공이다: Backend 는 0행을 저장하고 인기순으로 대체한다.
        #    그 밖에는 D(비선호 하드 제외 · 리뷰한 상품 제외 · 상한 · 중복 제거, 태그가 있으면 임베딩 + MMR)
        if has_no_input(rq):
            search = SearchResult(product_ids=[], query_text="", catalog_version_id=catalog_version_id)
            rule = "none:no-input"
            log.info("profile recipient=%s source_version=%s 입력 없음(비선호 0 · 취향 없음 · 리뷰 0) → 추천 0개 콜백, Backend 가 인기순 대체", rid, sv)
        else:
            search, rule = make_pool(rq, products, pool_size, catalog_version_id, dislike_mode=dislike_mode, scores=scores,
                                     pool_strategy=pool_strategy, tags=tags, embedding=embedding)
            if not search.product_ids:
                # 0개도 성공이다 — 빈 배열을 7.7로 보낸다. Backend는 저장된 추천이 없으면 인기순으로 대체한다(BE PR4 fallback).
                # 침묵(FAILED)하면 Backend 는 maximum-window(⚙6h) 뒤 복구 전송 때까지 기다리므로 보내는 쪽이 빠르다. BE 확인 요청 8번.
                # PENALTY 모드에서는 비선호로 0개가 될 수 없다 — 남는 원인은 재고(전량 unavailable)나 빈 카탈로그뿐
                why = "비선호·재고로 전부 제외" if dislike_mode is DislikeMode.EXCLUDE else "재고 없음·빈 카탈로그"
                log.warning("profile recipient=%s source_version=%s 풀 0개(%s) — 빈 배열 콜백, Backend가 인기순 대체", rid, sv, why)

        # 4) 결과
        outcome = ProfileOutcome(
            recipient_user_id=rid, source_version=sv, status=RunStatus.RESULT_READY, input_hash=h,
            validation=validation, search=search,
            prompt_version=prompt_version, validator_version=validator_version,
            tag_status=tag_status, llm_cost_usd=llm_cost,
        )
    except Exception as e:  # 업무 단계의 어떤 오류도 FAILED로 기록 — 백그라운드에서 조용히 죽지 않게
        log.exception("profile recipient=%s source_version=%s 실패", rid, sv)
        return _fail(rq, store, ErrorCode.PIPELINE_ERROR, f"{type(e).__name__}: {e}")

    # 5) 실행 기록 갱신 — RESULT_READY + 콜백 본문. 저장 실패는 결과를 FAILED로 (콜백을 보냈는데 우리 쪽에 기록이 없는 상태를 만들지 않기 위해)
    try:
        store.save(outcome)
    except Exception as e:
        log.exception("profile recipient=%s 저장 실패", rid)
        return _fail(rq, store, ErrorCode.STORE_FAILED, f"저장 실패: {type(e).__name__}: {e}", save=False)

    # 6) 수신자 프로필 — 한 사람당 한 행. 낮은 버전이 늦게 오면 DB가 무시한다(should_replace와 같은 규칙을 SQL로).
    #    실패하면 FAILED로 되돌린다: 콜백은 나갔는데 Chat이 읽을 프로필이 없는 상태를 만들지 않기 위해.
    try:
        recipient_store.upsert(from_outcome(rq, outcome, category_scores=scores))
    except Exception as e:
        log.exception("profile recipient=%s 수신자 프로필 저장 실패", rid)
        return _fail(rq, store, ErrorCode.STORE_FAILED, f"프로필 저장 실패: {type(e).__name__}: {e}")

    log.info("profile recipient=%s source_version=%s → RESULT_READY pool=%d/%d disliked=%d scores=%d tags=%d tagger=%s catalog_version=%s rule=%s",
             rid, sv, len(search.product_ids), pool_size, len(rq.disliked_categories), len(scores), len(tags), tag_status, catalog_version_id, rule)
    return outcome


class TagRun(NamedTuple):
    """태그 추출 한 번의 결과 — 검증 결과 · D 태그 · prompt_version · 상태(로그 · 집계용: ok · empty · bad_json · fail:<kind>) · 사용량."""

    validation: ValidationResult
    tags: list[selection.Tag]
    prompt_version: str | None
    status: str
    usage: TokenUsage | None = None


def _usage_fields(u: TokenUsage | None) -> str:
    if u is None:
        return "in_tok=? out_tok=?"
    return (f"in_tok={u.input_tokens} out_tok={u.output_tokens} load_ms={u.load_ms} in_ms={u.input_ms} out_ms={u.output_ms} "
            f"llm_ms={u.total_ms} stop={u.stop}" + (f" cost_usd={u.cost_usd}" if u.cost_usd is not None else ""))


def _extract_tags(rq: ProfileRequest, products: list[ProductRecord], tagger: TagExtractor,
                  disliked_names: list[str], spec: tagging.TagSpec | None = None) -> TagRun:
    """LLM 한 번 → 검증기 → TagRun. **실패해도 요청은 계속** — 태그 없이(빈 결과) 돌려준다.

    장애 추적: 한 줄 로그 `tag_extract <상태> recipient= source_version= model= …` 를 항상 남긴다 — 상태 · 원인 분류(timeout · connect ·
    http_404 …) · 토큰 · 시간(적재/입력/출력) · 입력 크기(리뷰 수 · 프롬프트 글자 수) · 남김/버림 수 · 축별 수 · 버림 사유별 수.
    원문(리뷰 글 · 취향 문장 · 모델 출력 · 태그 글자)은 로그에 남기지 않는다 — 개수 · 시간 · 모델 · 분류만."""
    rid, sv = rq.recipient_user_id, rq.source_version
    spec = spec or tagging.SPEC
    empty = ValidationResult(likes=[], key_features=[], dislikes=[], preferred_tags=[], disliked_tags=disliked_names, log=[])
    prep = tagging.prepare(rq, {p.productId: p for p in products})       # 가드레일: 개인정보 · 욕설 가림, 의미 없는 글 버림, 인젝션 의심 표시
    prompt, ratings = prep.prompt, prep.ratings
    pv = f"{spec.version}@{tagger.model}"
    g = prep.guard
    head = (f"recipient={rid} source_version={sv} model={tagger.model} prompt={spec.version} "
            f"reviews={len(rq.reviews)} pref={'y' if prep.has_pref else 'n'} prompt_chars={len(prompt)} "
            f"guard={g['version']}:{_counts(Counter(g['counts']))}")
    if not tagging.worth_llm(prep):                  # 근거가 될 것(취향 문장 · 4~5점 리뷰)이 없다 — 부르지 않는다(비용 0, 결과는 어차피 빈 태그)
        log.info("tag_extract skip_no_signal %s — 정리 뒤 취향 문장 없음 · 4~5점 리뷰 없음, LLM 안 부름", head)
        return TagRun(empty, [], pv, "skip_no_signal")
    t0 = time.perf_counter()
    try:
        reply = tagger.extract(spec.system, prompt, spec.schema)
    except TagExtractError as e:                     # 분류된 실패 — 시간 초과 · 연결 실패 · 모델 없음 · 잘림
        log.warning("tag_extract fail:%s %s wall_ms=%.0f %s err=%s — 태그 없이 진행", e.kind, head, (time.perf_counter() - t0) * 1000,
                    _usage_fields(e.usage), e)
        return TagRun(empty, [], None, f"fail:{e.kind}", e.usage)
    except Exception as e:                           # 분류 못 한 실패 — 스택을 남긴다(메시지엔 URL · 모델만, 원문 없음)
        log.warning("tag_extract fail:unknown %s wall_ms=%.0f err=%s — 태그 없이 진행", head, (time.perf_counter() - t0) * 1000,
                    type(e).__name__, exc_info=True)
        return TagRun(empty, [], None, "fail:unknown")
    wall_ms = (time.perf_counter() - t0) * 1000
    raw, usage = (reply.text, reply.usage) if isinstance(reply, TagReply) else (str(reply), None)
    parsed, parse_err = tagging.parse_report(raw)
    keep, drop = tagging.validate(parsed, ratings, prep.has_pref, spec)
    validation = tagging.to_validation(keep, drop, disliked_names)
    axes = Counter(t["axis"] for t in keep)
    drops = Counter(tagging.drop_reason_key(t.get("drop")) for t in drop)
    status = parse_err if parse_err in ("bad_json", "no_tags_list") else "empty" if not keep else "ok"
    if "injection_suspect" in g["counts"] and status == "ok":
        log.warning("tag_extract injection_suspect recipient=%s source_version=%s — 입력에 지시문 꼴, 태그 %d개는 검증기 통과분만", rid, sv, len(keep))
    level = logging.INFO if status == "ok" else logging.WARNING  # 깨진 출력 · 전부 버림은 품질 장애 — 경고로 올린다
    log.log(level, "tag_extract %s %s wall_ms=%.0f %s out_chars=%d parsed=%d kept=%d dropped=%d axes=%s drops=%s%s", status, head, wall_ms,
            _usage_fields(usage), len(raw), len(parsed), len(keep), len(drop), _counts(axes), _counts(drops),
            f" parse={parse_err}" if parse_err else "")
    return TagRun(validation, tagging.tags_of(validation), pv, status, usage)


def _counts(c: Counter) -> str:
    return ",".join(f"{k}:{v}" for k, v in c.most_common()) or "-"


def _fail(rq: ProfileRequest, store: ProfileRunStore, code: ErrorCode, reason: str, *, save: bool = True) -> ProfileOutcome:
    """FAILED 결과(분류 코드 + 사유)를 만들고(가능하면) 저장한다. 콜백은 보내지 않는다."""
    outcome = ProfileOutcome(recipient_user_id=rq.recipient_user_id, source_version=rq.source_version, status=RunStatus.FAILED,
                             input_hash=input_hash(rq), failure_code=code, failure_reason=reason, validator_version=VALIDATOR_VERSION_V1)
    if save:
        try:
            store.save(outcome)
        except Exception:
            log.exception("profile recipient=%s FAILED 기록 저장도 실패", rq.recipient_user_id)
    log.warning("profile recipient=%s source_version=%s → FAILED %s: %s", rq.recipient_user_id, rq.source_version, code, reason)
    return outcome
