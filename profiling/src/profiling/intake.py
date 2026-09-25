"""7.6 접수 라우터 — POST /api/internal/v1/ai/profile/extract-and-pool.

Transport 역할만 한다: 인증 → 스키마 검증 → 활성 카탈로그 확인 → 202 → 백그라운드로 pipeline.
업무 로직(무엇을 뽑고 어떻게 고르는지)은 pipeline.py에 있고, 바깥(파일·DB·HTTP)은 catalog.py · stores.py · backend.py에 있다.
이 파일은 그 둘을 "요청 한 건" 단위로 잇는다.

규칙 출처: 모델 API 설계 v3.2.7 §7.6 · 6단계 1.2
  202 SuccessResponse[ProfileAccepted]  profileStatus는 항상 PENDING, recipientUserId·sourceVersion은 요청 값 그대로
  400 INVALID_REQUEST     스키마 위반 — FastAPI 기본 422를 main.py의 exception_handler가 400 봉투로 바꾼다 (여기서는 안 함)
  401 UNAUTHORIZED        Authorization 헤더 없음 · Bearer 아님 · 토큰 불일치
  503 SERVICE_UNAVAILABLE 활성 카탈로그 없음
  실패한 분석은 7.6에도 7.7에도 FAILED를 보내지 않는다 (AI는 침묵, Backend가 판정)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request

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
# ---------------------------------------------------------------------------


def get_catalog(request: Request) -> CatalogReader:
    return request.app.state.catalog


def get_store(request: Request) -> ProfileRunStore:
    return request.app.state.store


def get_recipient_store(request: Request) -> RecipientProfileStore:
    return request.app.state.recipient_store


def get_backend(request: Request) -> BackendPort:
    return request.app.state.backend


def get_supervisor(request: Request) -> Supervisor:
    return request.app.state.supervisor


def _error(status: int, code: str, message: str) -> HTTPException:
    """HTTPException.detail에 {code, message}를 실어 보내면 main.py의 핸들러가 ErrorResponse 봉투로 만든다."""
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def require_service_token(
    authorization: Annotated[str | None, Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]  — FastAPI가 주입
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
# 백그라운드 작업 — 응답(202)을 보낸 뒤 같은 프로세스에서 실행된다.
# ---------------------------------------------------------------------------


def run_and_callback(
    rq: ProfileRequest, catalog: CatalogReader, store: ProfileRunStore, backend: BackendPort,
    pool_size: int, recipient_store: RecipientProfileStore,
) -> None:
    """pipeline.profile() → 결과가 RESULT_READY일 때만 7.7 콜백 → 콜백 결과를 실행 기록에 저장.

    - 여기서 나는 예외는 응답과 무관하므로(이미 202를 보냈다) 로그로만 남긴다. 죽은 백그라운드 작업은 조용히 사라지기 때문에
      반드시 잡아서 기록해야 한다.
    - FAILED는 콜백하지 않는다. Backend는 콜백이 오지 않으면 다음 디바운스 주기에 다시 7.6을 호출한다.
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

    _send_and_record(outcome, backend, store)


def resend_callback(outcome: ProfileOutcome, backend: BackendPort, store: ProfileRunStore) -> None:
    """**재분석 없이** 저장해 둔 결과를 7.7로 다시 보낸다 (Backend가 같은 sourceVersion으로 재전송했을 때).

    보내는 본문은 최초와 같다 — `backend.callback_body()`가 저장된 search에서 같은 상품 번호 목록을 만든다.
    그래서 재전송이 "같은 요청, 같은 결과"가 된다(3단계 §16.4). 그 사이 카탈로그가 바뀌어도 결과가 흔들리지 않는다.
    """
    log.info("7.7 재전송 recipient=%s source_version=%s (기존 %s · 재분석 없음)",
             outcome.recipient_user_id, outcome.source_version, outcome.status)
    _send_and_record(outcome, backend, store)


def _send_and_record(outcome: ProfileOutcome, backend: BackendPort, store: ProfileRunStore) -> None:
    """7.7 송신 → 그 결과를 실행 기록에 저장. 최초 전송과 재전송이 같은 경로를 쓴다."""
    rid, sv = outcome.recipient_user_id, outcome.source_version
    res = backend.send_profile_callback(outcome)
    log.info("7.7 콜백 결과 recipient=%s source_version=%s → %s%s", rid, sv, res.status,
             f" ({res.code}: {res.message})" if res.code else "")
    try:
        store.save(outcome.model_copy(update={"status": res.status, "callback_attempts": outcome.callback_attempts + 1,
                                              "failure_code": res.code, "failure_reason": res.message}))
    except Exception:
        log.exception("7.7 콜백 결과 저장 실패 recipient=%s source_version=%s (콜백은 %s)", rid, sv, res.status)


# ---------------------------------------------------------------------------
# 접수 단계 중복 판정 — 같은 (수신자, sourceVersion)이 또 왔을 때 무엇을 할까
# (3단계 구현 상세 §10.2 · Backend는 10분 PENDING 타임아웃 시 같은 번호로 최대 2회 재전송한다)
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


@router.post(
    EXTRACT_AND_POOL_PATH,
    status_code=202,
    response_model=SuccessResponse[ProfileAccepted],
    dependencies=[Depends(require_service_token)],
    summary="7.6 수신자 비동기 프로파일링 웹훅 (Backend → AI)",
)
async def extract_and_pool(
    body: ProfileExtractRequest,
    bg: BackgroundTasks,
    catalog: Annotated[CatalogReader, Depends(get_catalog)],
    store: Annotated[ProfileRunStore, Depends(get_store)],
    recipient_store: Annotated[RecipientProfileStore, Depends(get_recipient_store)],
    backend: Annotated[BackendPort, Depends(get_backend)],
    supervisor: Annotated[Supervisor, Depends(get_supervisor)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SuccessResponse[ProfileAccepted]:
    """접수만 하고 202를 돌려준다. 분석은 BackgroundTasks에서.

    순서:
      1) body 검증 — FastAPI가 이 함수에 들어오기 전에 ProfileExtractRequest로 검증한다 (위반 → 422 → main.py가 400으로)
      2) 토큰 — dependencies=[require_service_token]
      3) 활성 카탈로그 — 없으면 지금 503. 백그라운드에서 발견하면 Backend는 영영 모르기 때문에 접수 단계에서 걸러야 한다
      4) Supervisor에 제출 — 응답 뒤 슬롯 안에서 백그라운드 실행
      5) 202 — 요청 값 그대로 + PENDING
    같은 (수신자, 버전)의 중복 접수: Backend는 10분 PENDING 타임아웃 시 **같은 sourceVersion으로 최대 2회** 재전송한다(09-25 합의).
    그래서 접수 단계에서 기존 실행 기록을 보고 갈라진다 — decide() 참고. 어느 쪽이든 응답은 202 PENDING이다
    (Backend 입장에서는 "접수됐다"가 전부이고, 결과는 7.7로 간다).
    """
    try:
        catalog.active()
    except NoActiveCatalog as e:
        raise _error(503, "SERVICE_UNAVAILABLE", "활성 카탈로그가 없습니다.") from e

    unknown = unknown_fields(body)
    if unknown:  # 계약에 없는 필드 — 거부하지 않고 기록만. Backend가 필드를 추가했거나 이름이 어긋난 신호
        log.warning("%s recipient=%s source_version=%s unknown=%s — 무시하고 진행", ErrorCode.CONTRACT_7_6_UNKNOWN_FIELD,
                    body.recipientUserId, body.sourceVersion, unknown)

    rq = pipeline.to_internal(body)
    log.info("7.6 접수 recipient=%s source_version=%s disliked=%d reviews=%d pref=%s",
             body.recipientUserId, body.sourceVersion, len(body.dislikedCategories), len(body.reviews), body.giftPreference is not None)

    existing = _existing_run(store, rq)
    action, why = decide(existing, pipeline.input_hash(rq), stale_after_s=settings.RUNNING_STALE_S)
    log.info("7.6 중복 판정 recipient=%s source_version=%s → %s (%s)", body.recipientUserId, body.sourceVersion, action, why)
    if action == ANALYZE:
        supervisor.submit(bg, run_and_callback, rq, catalog, store, backend, settings.POOL_SIZE, recipient_store)   # 슬롯 안에서 실행
    elif action == RESEND:
        if existing.status is RunStatus.SUPERSEDED:   # 정상 흐름에서는 나오기 어렵다 — Backend가 더 새 버전을 이미 저장했다는 뜻
            log.warning("7.7 재전송 대상이 SUPERSEDED recipient=%s source_version=%s — Backend가 다시 409를 줄 수 있다",
                        body.recipientUserId, body.sourceVersion)
        supervisor.submit(bg, resend_callback, existing, backend, store)      # 재분석 없음 — 콜백만

    return SuccessResponse(
        message="프로파일 분석이 시작되었습니다.",
        data=ProfileAccepted(recipientUserId=body.recipientUserId, sourceVersion=body.sourceVersion),
    )
