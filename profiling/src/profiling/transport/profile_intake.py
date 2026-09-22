"""7.6 접수 라우터 — POST /api/internal/v1/ai/profile/extract-and-pool.

Transport 역할만 한다: 인증 → 스키마 검증 → 활성 카탈로그 확인 → 202 → 백그라운드로 pipeline.
업무 로직(무엇을 뽑고 어떻게 고르는지)은 profile/pipeline.py에 있고, 바깥(파일·HTTP)은 adapters에 있다.
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
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request

from profiling.config.settings import Settings, get_settings
from profiling.profile import pipeline
from profiling.profile.ports import (
    BackendPort,
    CatalogReader,
    NoActiveCatalog,
    ProfileRunStore,
)
from profiling.profile.types import ProfileRequest, RunStatus
from profiling.runtime.supervisor import Supervisor
from profiling.transport.schemas import (
    ProfileAccepted,
    ProfileExtractRequest,
    SuccessResponse,
)

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


def run_and_callback(rq: ProfileRequest, catalog: CatalogReader, store: ProfileRunStore, backend: BackendPort, pool_size: int = 30) -> None:
    """pipeline.profile() → 결과가 RESULT_READY일 때만 7.7 콜백.

    - 여기서 나는 예외는 응답과 무관하므로(이미 202를 보냈다) 로그로만 남긴다. 죽은 백그라운드 작업은 조용히 사라지기 때문에
      반드시 잡아서 기록해야 한다.
    - FAILED는 콜백하지 않는다. Backend는 콜백이 오지 않으면 다음 디바인스 주기에 다시 7.6을 호출한다.
    - 콜백 결과는 RunStatus로 돌아온다(DELIVERED·SUPERSEDED·FAILED·RESULT_READY). 오늘은 로그만 남기고, 실행 기록 상태 갱신·재시도는 #23
      (DB adapter가 생기면 store.save(outcome.model_copy(update={"status": result}))로 이어진다).
    """
    rid = rq.recipient_user_id
    try:
        outcome = pipeline.profile(rq, catalog=catalog, store=store, pool_size=pool_size)
    except Exception:  # 백그라운드에서는 무엇이든 잡아 기록한다
        log.exception("profile 실패 recipient=%s source_version=%s", rid, rq.source_version)
        return

    if outcome.status != RunStatus.RESULT_READY:
        log.warning("profile 결과 %s recipient=%s reason=%s — 콜백 없음", outcome.status, rid, outcome.failure_reason)
        return

    result = backend.send_profile_callback(outcome)
    log.info("7.7 콜백 결과 recipient=%s source_version=%s → %s", rid, rq.source_version, result)


# ---------------------------------------------------------------------------
# 7.6 엔드포인트
# ---------------------------------------------------------------------------


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
    같은 수신자의 중복 접수: 오늘은 그대로 다시 돌린다(마지막 결과가 덮어씀). "진행 중이면 새 분석 없음"은 실행 기록(DB)이 생긴 뒤.
    """
    try:
        catalog.active()
    except NoActiveCatalog as e:
        raise _error(503, "SERVICE_UNAVAILABLE", "활성 카탈로그가 없습니다.") from e

    rq = pipeline.to_internal(body)
    supervisor.submit(bg, run_and_callback, rq, catalog, store, backend, settings.POOL_SIZE)   # 슬롯 안에서 실행 (Supervisor)
    log.info("7.6 접수 recipient=%s source_version=%s disliked=%d reviews=%d pref=%s",
             body.recipientUserId, body.sourceVersion, len(body.dislikedCategories), len(body.reviews), body.giftPreference is not None)

    return SuccessResponse(
        message="프로파일 분석이 시작되었습니다.",
        data=ProfileAccepted(recipientUserId=body.recipientUserId, sourceVersion=body.sourceVersion),
    )
