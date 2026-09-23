"""파이프라인 — 7.6 요청 한 건을 끝까지 처리한다. 4단계 2.2 `profile()` 의사코드의 자리 (Application Service).

이 파일은 "무엇을 어떤 순서로"만 안다. 바깥(파일·DB·HTTP)은 ports의 모양으로만 받고(인자 catalog·store), 구현 모듈을 import하지 않는다.
그래서 단위 테스트는 ports 모양의 가짜(dict 하나짜리 클래스)를 넣어 DB 없이 돈다.

v1(지금): 취향 문장 None · 리뷰 [] → 추출·검증을 건너뛰고, 비선호 카테고리를 뺀 상품 풀 30개를 만든다.
v3: needs_model()이 True인 분기에 카탈로그 조인 → ProfileModel.analyze()(모델) → 검증기 → Search 호출이 들어온다. 나머지 흐름은 그대로.

흐름 (한 건):
  api.run_and_callback ──▶ profile(rq, catalog=, store=, recipient_store=)
                            0) store.save(RUNNING)         실행 기록 시작 — profile_runs 한 행 (input_hash 포함). 저장 못 하면 FAILED·콜백 없음
                            1) catalog.active()            활성 카탈로그 (없으면 NoActiveCatalog — api가 접수 단계에서 이미 걸렀지만 여기서도 FAILED로 기록)
                            2) validation 만들기           v1: 비선호 이름만 disliked_tags에, 나머지 빈 값
                            3) build_pool()                비선호 제외 · 재고 없음(unavailable)만 제외 · 조회수순 pool_size개  (결정 a)
                            4) ProfileOutcome(RESULT_READY)
                            5) store.save(outcome)         실행 기록 갱신 — callback_payload·hash가 여기서 DB에 먼저 커밋된다 (§16.4)
                            6) recipient_store.upsert()    수신자 프로필 (recipient_profiles) — 낮은 버전이면 DB가 무시
                          ◀── outcome  (RESULT_READY면 api가 7.7 콜백, FAILED면 침묵)
"""

from __future__ import annotations

import hashlib
import logging
from uuid import UUID

from profiling.ports import (
    CatalogReader,
    NoActiveCatalog,
    ProfileRunStore,
    RecipientProfileStore,
)
from profiling.schemas import ProductRecord, ProfileExtractRequest
from profiling.types import (
    DislikedCategory,
    ErrorCode,
    ProfileOutcome,
    ProfileRequest,
    Review,
    RunStatus,
    SearchResult,
    ValidationResult,
    from_outcome,
)

log = logging.getLogger(__name__)

# 관측 라벨 — 어떤 규칙으로 만든 결과인지 저장해 두면 나중에 결과를 비교·회귀할 때 "언제 규칙이 바뀌었나"를 알 수 있다
VALIDATOR_VERSION_V1 = "v1-skip"      # 검증기 없이 비선호만 반영
POOL_RULE_V1 = "v1-view-count"        # 풀 정렬 규칙: 조회수 내림차순, 동점 productId 오름차순 (정렬 키는 아직 논의 중 — 09-23)


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


def needs_model(rq: ProfileRequest) -> bool:
    """취향 문장이 있거나 리뷰가 하나라도 있으면 모델이 읽을 자료가 있는 것 → True. 둘 다 없으면(v1) False.

    v1에서는 True여도 모델 단계가 아직 없어 v1 경로로 처리하고 경고를 남긴다 (profile() 참고).
    """
    return rq.gift_preference is not None or bool(rq.reviews)


# ---------------------------------------------------------------------------
# 3) 풀 만들기 — v1의 "검색". 팀원 Search가 오면 이 함수 호출이 search.search(...)로 바뀐다 (결정 a)
# ---------------------------------------------------------------------------


def build_pool(rq: ProfileRequest, products: list[ProductRecord], pool_size: int, catalog_version_id: UUID) -> SearchResult:
    """비선호 카테고리를 뺀 상품을 **조회수 내림차순**으로 pool_size개.

    재고(availability, 3값): **unavailable(재고 없음)만 뺀다.** unknown(재고 정보가 없는 상품)은 풀에 남긴다 — 09-23 Backend 합의.
    실제 재고를 아는 쪽은 Backend이므로 최종 판단을 Backend가 한다. 우리는 unknown을 available로 바꿔 쓰지 않고(검색기와 같은 규칙)
    "재고 없음으로 확인된 것만 제외"할 뿐이다. 패키지로 적재한 카탈로그는 전건 unknown이라, 이 규칙이 아니면 풀이 0건이 된다.

    정렬 기준(결정 a, 09-22 Backend 합의): 상품 목록 export의 조회수(viewCount) 내림차순. 동점은 productId 오름차순(카탈로그 순서)으로
    묶어 같은 입력이면 항상 같은 결과가 나오게 한다 — 임의성(random)은 넣지 않는다. Backend는 초기에 임의 조회수를 넣어 보내므로
    v1 배포 시점에도 정렬이 동작한다. 팀원 Search가 붙는 v3에서 이 함수 호출이 search.search(...)로 바뀐다.
    query_text는 v1에서 빈 문자열 — Search가 붙으면 검증기의 preferred_tags를 이어 붙인 질의가 들어간다.
    """
    disliked_ids = {c.category_id for c in rq.disliked_categories}
    disliked_names = {c.category_name for c in rq.disliked_categories}
    candidates = [
        p for p in products
        if p.availability != "unavailable"          # unknown은 남긴다 — 재고 판단은 Backend 몫
        # ID가 정본, 이름은 보조 — Backend가 준 ID와 카탈로그 ID 체계가 어긋나는 사고(ID는 다른데 이름은 같음)까지 막는다
        and p.categoryId not in disliked_ids and p.categoryName not in disliked_names
    ]
    candidates.sort(key=lambda p: (-p.viewCount, p.productId))
    picked = [p.productId for p in candidates[:pool_size]]
    return SearchResult(product_ids=picked, query_text="", catalog_version_id=catalog_version_id)


# ---------------------------------------------------------------------------
# 4) 한 건 처리 — 접수(api)가 백그라운드에서 부른다
# ---------------------------------------------------------------------------


def profile(
    rq: ProfileRequest, *, catalog: CatalogReader, store: ProfileRunStore,
    recipient_store: RecipientProfileStore, pool_size: int = 30,
) -> ProfileOutcome:
    """7.6 요청 한 건 → ProfileOutcome (저장까지). 예외를 밖으로 내지 않고 FAILED로 돌려준다.

    반환의 status가 RESULT_READY면 호출자(api.run_and_callback)가 7.7 콜백을 보내고, FAILED면 보내지 않는다(문서 1: AI는 침묵).
    catalog·store·recipient_store는 ports 모양이면 무엇이든 된다(파일/DB → 테스트 가짜).
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
        # 2) 검증 결과 — v1은 검증기를 돌리지 않는다. Backend 명시 비선호 이름만 disliked_tags에 (병합 규칙: Backend 목록이 앞)
        if needs_model(rq):
            # v3 자리: 조인 → ProfileModel.analyze() → validator. 아직 없으므로 자료를 무시하고 v1 경로로 간다는 것을 로그로 남긴다
            log.warning("profile recipient=%s: 취향/리뷰(%d개)가 있으나 모델 단계 미구현 — v1 경로(비선호만)로 처리", rid, len(rq.reviews))
        validation = ValidationResult(
            likes=[], key_features=[], dislikes=[],
            preferred_tags=[],
            disliked_tags=[c.category_name for c in rq.disliked_categories],
            log=[],
        )

        # 3) 풀 30개
        search = build_pool(rq, products, pool_size, catalog_version_id)

        # 4) 결과
        outcome = ProfileOutcome(
            recipient_user_id=rid, source_version=sv, status=RunStatus.RESULT_READY, input_hash=h,
            validation=validation, search=search,
            prompt_version=None, validator_version=VALIDATOR_VERSION_V1,
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
        recipient_store.upsert(from_outcome(rq, outcome))
    except Exception as e:
        log.exception("profile recipient=%s 수신자 프로필 저장 실패", rid)
        return _fail(rq, store, ErrorCode.STORE_FAILED, f"프로필 저장 실패: {type(e).__name__}: {e}")

    log.info("profile recipient=%s source_version=%s → RESULT_READY pool=%d/%d disliked=%d catalog_version=%s rule=%s",
             rid, sv, len(search.product_ids), pool_size, len(rq.disliked_categories), catalog_version_id, POOL_RULE_V1)
    return outcome


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
