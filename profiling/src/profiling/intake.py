"""7.6 접수 라우터 — POST /api/internal/v1/ai/profile/extract-and-pool.

Transport 역할만 한다: 인증 → 스키마 검증 → 활성 카탈로그 확인 → Supervisor 큐 → 202. 실행은 Supervisor 의 워커 스레드가 한다.
업무 로직(무엇을 뽑고 어떻게 고르는지)은 pipeline.py에 있고, 바깥(파일·DB·HTTP)은 catalog.py · stores.py · backend.py에 있다.
이 파일은 그 둘을 "요청 한 건" 단위로 잇는다.

규칙 출처: 모델 API 설계 v3.2.7 §7.6 · 6단계 1.2
  202 SuccessResponse[ProfileAccepted]  profileStatus는 항상 PENDING, recipientUserId·sourceVersion은 요청 값 그대로
  400 INVALID_REQUEST     스키마 위반 — FastAPI 기본 422를 main.py의 exception_handler가 400 봉투로 바꾼다 (여기서는 안 함)
  401 UNAUTHORIZED        Authorization 헤더 없음 · Bearer 아님 · 토큰 불일치
  503 SERVICE_UNAVAILABLE 활성 카탈로그 없음(Retry-After 300) · 접수 대기열 가득(Retry-After QUEUE_FULL_RETRY_AFTER_S, 09-28)
                          — Retry-After 는 Backend 가 읽지 않는다(통합 수정점 v0.7, 10-08 실측). 503 은 재시도 가능 실패로 틱 중단 + 디바운스 재시작 1회
  실패한 분석은 7.6에도 7.7에도 FAILED를 보내지 않는다 (AI는 침묵, Backend가 판정)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from profiling import pipeline
from profiling.ports import (
    BackendPort,
    CatalogReader,
    NoActiveCatalog,
    ProfileRunStore,
    RecipientProfileStore,
)
from profiling.schemas import (
    ProfileAccepted,
    ProfileExtractRequest,
    SuccessResponse,
    unknown_fields,
)
from profiling.settings import Settings, get_settings
from profiling.supervisor import Supervisor
from profiling.types import ErrorCode, ProfileOutcome, ProfileRequest, RunStatus

log = logging.getLogger(__name__)

router = APIRouter(tags=["profile"])

# 문서 1 §7.6 경로. 접두사 /api/internal/v1 는 팀원 앱과 합칠 때 라우터 prefix로 뺄 수 있다.
EXTRACT_AND_POOL_PATH = "/api/internal/v1/ai/profile/extract-and-pool"


# ---------------------------------------------------------------------------
# 의존성 — main.py가 lifespan에서 app.state에 넣어 둔 adapter를 꺼낸다.
# 라우터는 "어떤 구현인지" 모른다. 테스트는 app.state에 가짜를 넣거나 dependency_overrides로 바꾼다.
# 전부 async — 동기 의존성은 anyio 기본 스레드풀(토큰 40)을 거치는데, 그 토큰을 실행 대기가 잡으면 접수가 굶는다(09-28 부하 시험).
# ---------------------------------------------------------------------------


async def get_catalog(request: Request) -> CatalogReader:
    return request.app.state.catalog


async def get_store(request: Request) -> ProfileRunStore:
    return request.app.state.store


async def get_recipient_store(request: Request) -> RecipientProfileStore:
    return request.app.state.recipient_store


async def get_backend(request: Request) -> BackendPort:
    return request.app.state.backend


async def get_supervisor(request: Request) -> Supervisor:
    return request.app.state.supervisor


async def get_io_limiter(request: Request) -> anyio.CapacityLimiter:
    """접수의 카탈로그 폴링 같은 짧은 DB 문장용 스레드 한도 — 기본 스레드풀과 분리(main.lifespan 이 만든다)."""
    return request.app.state.io_limiter


async def settings_dep() -> Settings:
    """Depends(get_settings) 는 동기라 스레드풀을 탄다 — async 로 감싼다."""
    return get_settings()


def _error(status: int, code: str, message: str, *, retry_after: int | None = None) -> HTTPException:
    """HTTPException.detail에 {code, message[, retryAfter]}를 실어 보내면 main.py의 핸들러가 ErrorResponse 봉투로 만든다.
    retry_after 를 주면 503 의 Retry-After 헤더가 그 값이 된다(없으면 settings.RETRY_AFTER_S)."""
    detail: dict[str, Any] = {"code": code, "message": message}
    if retry_after is not None:
        detail["retryAfter"] = retry_after
    return HTTPException(status_code=status, detail=detail)


async def require_service_token(
    authorization: Annotated[str | None, Header()] = None,
    settings: Annotated[Settings, Depends(settings_dep)] = None,  # type: ignore[assignment]  — FastAPI가 주입
) -> None:
    """`Authorization: Bearer <service_token>` 확인 (문서 1 §7.6 Header). 틀리면 401 UNAUTHORIZED.

    403 FORBIDDEN(접근 권한 없음)은 토큰은 맞지만 권한이 없는 경우인데, 서비스 토큰 하나뿐인 지금은 발생하지 않는다.
    """
    expected = settings.SERVICE_TOKEN
    if not expected:  # 토큰을 설정하지 않은 로컬 개발 환경에서는 검사하지 않는다 (.env.example에 dev-token이 있으므로 보통은 설정됨)
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise _error(401, "UNAUTHORIZED", "서비스 토큰이 없습니다.")
    if authorization.removeprefix("Bearer ").strip() != expected:
        raise _error(401, "UNAUTHORIZED", "서비스 토큰이 올바르지 않습니다.")


# ---------------------------------------------------------------------------
# 백그라운드 작업 — Supervisor 워커 스레드가 큐에서 꺼내 실행한다(응답과 무관).
# ---------------------------------------------------------------------------


def run_and_callback(
    rq: ProfileRequest, catalog: CatalogReader, store: ProfileRunStore, backend: BackendPort,
    pool_size: int, recipient_store: RecipientProfileStore, *, max_attempts: int = 3, backoff_s: float = 0.5,
) -> None:
    """pipeline.profile() → 결과가 RESULT_READY일 때만 7.7 콜백 → 콜백 결과를 실행 기록에 저장.

    - 여기서 나는 예외는 응답과 무관하므로(이미 202를 보냈다) 로그로만 남긴다. 죽은 백그라운드 작업은 조용히 사라지기 때문에
      반드시 잡아서 기록해야 한다.
    - FAILED는 콜백하지 않는다. Backend는 콜백이 오지 않으면 PENDING이 maximum-window(⚙6h)를 넘긴 뒤 **같은 번호로 복구 전송**한다
      (통합 수정점 v0.7 ⑤, 10-08 실측). 그때 decide()가 FAILED 행을 보고 다시 분석한다.
    - 콜백 결과(RunStatus: DELIVERED·SUPERSEDED·FAILED·RESULT_READY)를 같은 행에 저장한다 — profile_runs.status·callback_attempts.
      RESULT_READY(5xx·네트워크)면 행은 "결과 있음·미전달"로 남아 재전송 대상이 된다. 재시도·백오프 자체는 #23.
    """
    rid = rq.recipient_user_id
    try:
        outcome = pipeline.profile(rq, catalog=catalog, store=store, recipient_store=recipient_store, pool_size=pool_size)
    except Exception:  # 백그라운드에서는 무엇이든 잡아 기록한다
        log.exception("profile 실패 recipient=%s source_version=%s", rid, rq.source_version)
        return

    if outcome.status != RunStatus.RESULT_READY:
        log.warning("profile 결과 %s recipient=%s reason=%s — 콜백 없음", outcome.status, rid, outcome.failure_reason)
        return

    _send_and_record(outcome, backend, store, max_attempts=max_attempts, backoff_s=backoff_s)


def resend_callback(
    outcome: ProfileOutcome, backend: BackendPort, store: ProfileRunStore, *,
    max_attempts: int = 3, backoff_s: float = 0.5, sleep: Callable[[float], None] = time.sleep,
) -> None:
    """**재분석 없이** 저장해 둔 결과를 7.7로 다시 보낸다 (Backend가 같은 sourceVersion으로 재전송했을 때).

    보내는 본문은 최초와 같다 — `backend.callback_body()`가 저장된 search에서 같은 상품 번호 목록을 만든다.
    그래서 재전송이 "같은 요청, 같은 결과"가 된다(3단계 §16.4). 그 사이 카탈로그가 바뀌어도 결과가 흔들리지 않는다.
    """
    log.info("7.7 재전송 recipient=%s source_version=%s (기존 %s · 재분석 없음)",
             outcome.recipient_user_id, outcome.source_version, outcome.status)
    _send_and_record(outcome, backend, store, max_attempts=max_attempts, backoff_s=backoff_s, sleep=sleep)


def _send_and_record(
    outcome: ProfileOutcome, backend: BackendPort, store: ProfileRunStore, *,
    max_attempts: int = 3, backoff_s: float = 0.5, sleep: Callable[[float], None] = time.sleep,
) -> None:
    """7.7 송신 → 그 결과를 실행 기록에 저장. 최초 전송과 재전송이 같은 경로를 쓴다.

    **즉시 재시도**: 결과가 RESULT_READY(5xx·타임아웃·연결 실패)면 같은 슬롯 안에서 backoff_s × (1, 4, 16…) 초 기다렸다
    다시 보낸다 — 최대 max_attempts 회(기본 3회: 0.5초·2초 뒤). 그래도 안 되면 RESULT_READY 로 남겨 Backend 가 같은 번호로
    다시 요청할 때 재전송한다(decide → RESEND). 200·409·4xx 는 그 자리에서 끝난다 — 다시 보내도 답이 같다.
    저장은 마지막에 한 번, callback_attempts 는 실제로 보낸 횟수만큼 더한다. sleep 은 시험에서 바꿔 끼운다.
    """
    rid, sv = outcome.recipient_user_id, outcome.source_version
    attempts = 0
    while True:
        attempts += 1
        res = backend.send_profile_callback(outcome)
        log.info("7.7 콜백 결과 recipient=%s source_version=%s 시도 %d/%d → %s%s", rid, sv, attempts, max_attempts, res.status,
                 f" ({res.code}: {res.message})" if res.code else "")
        if res.status is not RunStatus.RESULT_READY or attempts >= max_attempts:
            break
        wait = backoff_s * (4 ** (attempts - 1))
        log.warning("7.7 미전달 recipient=%s source_version=%s — %.1f초 뒤 다시 보낸다 (%d/%d)", rid, sv, wait, attempts + 1, max_attempts)
        sleep(wait)
    try:
        store.save(outcome.model_copy(update={"status": res.status, "callback_attempts": outcome.callback_attempts + attempts,
                                              "failure_code": res.code, "failure_reason": res.message}))
    except Exception:
        log.exception("7.7 콜백 결과 저장 실패 recipient=%s source_version=%s (콜백은 %s)", rid, sv, res.status)


# ---------------------------------------------------------------------------
# 접수 단계 중복 판정 — 같은 (수신자, sourceVersion)이 또 왔을 때 무엇을 할까
# (3단계 구현 상세 §10.2 · Backend는 PENDING이 maximum-window(⚙6h)를 넘으면 같은 번호로 **복구 전송**한다 — 창마다 반복, 틱당 ⚙50.
#  통합 수정점 v0.7 ⑤, 10-08 실측. 옛 모델 "10분 타임아웃 · 같은 번호 최대 2회"(09-25)는 폐기됐다)
# ---------------------------------------------------------------------------

ANALYZE, RESEND, SKIP = "analyze", "resend", "skip"


def decide(existing: ProfileOutcome | None, input_hash: str, *, stale_after_s: int, now: datetime | None = None) -> tuple[str, str]:
    """(무엇을 할지, 왜) — 순수 함수라 DB 없이 시험한다.

      analyze  분석을 돌린다 (처음이거나, 실패했거나, 입력이 달라졌거나, RUNNING이 죽었다)
      resend   저장된 결과를 7.7로 다시 보내기만 한다 — 재분석 없음
      skip     아무것도 하지 않는다 (이미 돌고 있다)

    입력 해시 비교가 안전장치다. Backend 재전송은 같은 본문이어야 하고, 본문이 달라졌다면 같은 번호라도
    **옛 결과를 다시 보내면 틀린 답**이 되므로 다시 분석한다.
    """
    if existing is None:
        return ANALYZE, "첫 접수"
    if existing.input_hash and existing.input_hash != input_hash:
        return ANALYZE, "같은 sourceVersion인데 본문이 다름 — 재분석"
    if existing.status is RunStatus.RUNNING:
        age = _age_s(existing.updated_at, now)
        if age is not None and age > stale_after_s:
            return ANALYZE, f"RUNNING이 {age:.0f}초째 그대로 — 죽은 실행으로 보고 재분석"
        return SKIP, "이미 분석 중"
    if existing.status is RunStatus.FAILED:
        return ANALYZE, "지난 실행이 실패 — 재분석"
    if existing.search is None:                      # 결과 상태인데 콜백 본문이 없다 (DB CHECK가 막지만 방어)
        return ANALYZE, f"{existing.status}인데 저장된 결과가 없음 — 재분석"
    return RESEND, f"{existing.status} — 저장된 결과 재전송"


def _age_s(updated_at: datetime | None, now: datetime | None) -> float | None:
    """그 행이 마지막으로 갱신된 뒤 지난 시간(초). 시각을 모르면 None(= 신선한 것으로 본다)."""
    if updated_at is None:
        return None
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - updated_at).total_seconds()


# ---------------------------------------------------------------------------
# 7.6 엔드포인트
# ---------------------------------------------------------------------------


def _existing_run(store: ProfileRunStore, rq: ProfileRequest) -> ProfileOutcome | None:
    """그 (수신자, 버전)의 기존 실행 기록. 조회가 실패하면 None — 판정을 못 한다고 접수를 막지는 않는다(분석을 한 번 더 돌릴 뿐)."""
    try:
        return store.get_run(rq.recipient_user_id, rq.source_version)
    except Exception:
        log.exception("중복 판정용 조회 실패 recipient=%s source_version=%s — 평소대로 분석한다",
                      rq.recipient_user_id, rq.source_version)
        return None


def dispatch(
    rq: ProfileRequest, catalog: CatalogReader, store: ProfileRunStore, backend: BackendPort,
    pool_size: int, recipient_store: RecipientProfileStore, stale_after_s: int,
    callback_max_attempts: int = 3, callback_backoff_s: float = 0.5,
) -> None:
    """**잠금 → 판정 → 실행**을 한 덩어리로. Supervisor 워커 스레드(슬롯) 하나에서 끝까지 돈다.

    판정을 접수(HTTP) 쪽에 두면 "읽고 나서 쓰기까지" 사이가 벌어져, 같은 (수신자, 버전)이 동시에 오면
    둘 다 "기록 없음"을 보고 둘 다 분석한다. Backend 는 복구 전송으로 같은 번호를 다시 보내므로(v0.7 ⑤) 실제로 겹칠 수 있다.
    그래서 판정과 그 실행을 같은 잠금 안에 넣는다 — 잠금을 못 얻으면 다른 실행이 그 키를 처리 중이라는 뜻이니
    이번 것은 할 일이 없다.
    """
    rid, sv = rq.recipient_user_id, rq.source_version
    with store.run_lock(rid, sv) as got:
        if not got:
            return
        existing = _existing_run(store, rq)
        action, why = decide(existing, pipeline.input_hash(rq), stale_after_s=stale_after_s)
        log.info("7.6 중복 판정 recipient=%s source_version=%s → %s (%s)", rid, sv, action, why)
        if action == ANALYZE:
            run_and_callback(rq, catalog, store, backend, pool_size, recipient_store,
                             max_attempts=callback_max_attempts, backoff_s=callback_backoff_s)
        elif action == RESEND and existing is not None:
            if existing.status is RunStatus.SUPERSEDED:   # 정상 흐름에서는 나오기 어렵다 — Backend 가 더 새 버전을 이미 저장했다는 뜻
                log.warning("7.7 재전송 대상이 SUPERSEDED recipient=%s source_version=%s — Backend 가 다시 409 를 줄 수 있다", rid, sv)
            resend_callback(existing, backend, store, max_attempts=callback_max_attempts, backoff_s=callback_backoff_s)


@router.post(
    EXTRACT_AND_POOL_PATH,
    status_code=202,
    response_model=SuccessResponse[ProfileAccepted],
    dependencies=[Depends(require_service_token)],
    summary="7.6 수신자 비동기 프로파일링 웹훅 (Backend → AI)",
)
async def extract_and_pool(
    body: ProfileExtractRequest,
    catalog: Annotated[CatalogReader, Depends(get_catalog)],
    store: Annotated[ProfileRunStore, Depends(get_store)],
    recipient_store: Annotated[RecipientProfileStore, Depends(get_recipient_store)],
    backend: Annotated[BackendPort, Depends(get_backend)],
    supervisor: Annotated[Supervisor, Depends(get_supervisor)],
    settings: Annotated[Settings, Depends(settings_dep)],
    io_limiter: Annotated[anyio.CapacityLimiter, Depends(get_io_limiter)],
) -> SuccessResponse[ProfileAccepted]:
    """접수만 하고 202를 돌려준다. 분석은 Supervisor 워커 스레드에서.

    순서:
      1) body 검증 — FastAPI가 이 함수에 들어오기 전에 ProfileExtractRequest로 검증한다 (위반 → 422 → main.py가 400으로)
      2) 토큰 — dependencies=[require_service_token]
      3) 활성 카탈로그 — 없으면 지금 503. 백그라운드에서 발견하면 Backend는 영영 모르기 때문에 접수 단계에서 걸러야 한다
      4) Supervisor 큐에 제출 — 가득이거나 종료 중이면 503 + Retry-After(QUEUE_FULL_RETRY_AFTER_S). 기다리지 않는다
      5) 202 — 요청 값 그대로 + PENDING
    같은 (수신자, 버전)의 중복 접수: Backend는 PENDING이 maximum-window(⚙6h)를 넘으면 **같은 sourceVersion으로 복구 전송**한다
    (통합 수정점 v0.7 ⑤, 10-08 실측 — 창마다 반복, 틱당 ⚙50. 새 변경이 있으면 복구 대신 새 번호).
    그 판정(decide)은 워커 스레드의 슬롯 안에서 한다 — dispatch() 참고. 어느 쪽이든 응답은 202 PENDING이다
    (Backend 입장에서는 "접수됐다"가 전부이고, 결과는 7.7로 간다).
    """
    try:
        await anyio.to_thread.run_sync(catalog.active, limiter=io_limiter)   # TTL 밖이면 DB 1문장 — 기본 스레드풀·이벤트 루프 둘 다 안 잡는다
    except NoActiveCatalog as e:
        raise _error(503, "SERVICE_UNAVAILABLE", "활성 카탈로그가 없습니다.") from e

    unknown = unknown_fields(body)
    if unknown:  # 계약에 없는 필드 — 거부하지 않고 기록만. Backend가 필드를 추가했거나 이름이 어긋난 신호
        log.warning("%s recipient=%s source_version=%s unknown=%s — 무시하고 진행", ErrorCode.CONTRACT_7_6_UNKNOWN_FIELD,
                    body.recipientUserId, body.sourceVersion, unknown)

    rq = pipeline.to_internal(body)
    log.info("7.6 접수 recipient=%s source_version=%s disliked=%d reviews=%d pref=%s",
             body.recipientUserId, body.sourceVersion, len(body.dislikedCategories), len(body.reviews), body.giftPreference is not None)

    # 중복 판정은 **슬롯 안에서** 한다(dispatch) — 판정과 실행 사이가 벌어지면 같은 요청이 동시에 와서 분석이 두 벌 돈다
    accepted = supervisor.submit(dispatch, rq, catalog, store, backend, settings.POOL_SIZE, recipient_store, settings.RUNNING_STALE_S,
                                 settings.CALLBACK_MAX_ATTEMPTS, settings.CALLBACK_BACKOFF_S)
    if not accepted:   # 큐 가득·종료 중 — 기다리지 않고 거절. Backend 는 Retry-After 를 읽지 않고 503 을 재시도 가능 실패로 본다:
                       # 틱 중단 + 디바운스 재시작 1회, 두 번째면 접음(v0.7 ②③). 그래서 QUEUE_MAX 는 BE 틱당 일반 ⚙100 + 복구 ⚙50 보다 커야 한다
        log.warning("7.6 거절 recipient=%s source_version=%s — 접수 대기열 가득(%d) 또는 종료 중 → 503 Retry-After %ds",
                    body.recipientUserId, body.sourceVersion, settings.QUEUE_MAX, settings.QUEUE_FULL_RETRY_AFTER_S)
        raise _error(503, "SERVICE_UNAVAILABLE", "접수 대기열이 가득 찼습니다.", retry_after=settings.QUEUE_FULL_RETRY_AFTER_S)

    return SuccessResponse(
        message="프로파일 분석이 시작되었습니다.",
        data=ProfileAccepted(recipientUserId=body.recipientUserId, sourceVersion=body.sourceVersion),
    )
