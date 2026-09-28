"""앱 조립 — 어떤 adapter 구현을 끼울지 정하는 유일한 곳.

실행:  uv run uvicorn profiling.main:app --port 8000 --reload
확인:  curl localhost:8000/health

여기서만 구체 클래스(DbCatalogReader·FileCatalogReader · Db*Store · HttpBackendPort)를 import한다. 라우터(intake.py)와 업무(pipeline.py)는 app.state에 든 객체를 ports의 모양으로만 쓴다.

app.state에 두는 것 (lifespan에서 1회 생성):
  catalog          CatalogReader        DbCatalogReader(ai_catalog) 또는 FileCatalogReader — CATALOG_SOURCE 설정
  store            ProfileRunStore      DbProfileRunStore (ai_profile.profile_runs) — 시작 시 끊긴 RUNNING 정리
  recipient_store  RecipientProfileStore  DbRecipientProfileStore (ai_profile.recipient_profiles)
  backend          BackendPort          HttpBackendPort
  supervisor       Supervisor            워커 스레드 + 상한 큐 — start() 하고, 종료 때 stop()
  io_limiter       anyio.CapacityLimiter  접수 폴링·/health 의 짧은 DB 문장용(기본 스레드풀과 분리)
  engine           sqlalchemy Engine    시작 시 연결 확인, 종료 시 dispose
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from profiling import intake
from profiling.backend import HttpBackendPort
from profiling.catalog import DbCatalogReader, FileCatalogReader
from profiling.ports import NoActiveCatalog
from profiling.schemas import ErrorBody, ErrorResponse
from profiling.settings import Settings, get_settings
from profiling.stores import (
    DbProfileRunStore,
    DbRecipientProfileStore,
)
from profiling.supervisor import Supervisor

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 시작·종료 — adapter 생성은 여기서 1회. 요청마다 만들지 않는다.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # 저장소 — PostgreSQL 하나뿐. 연결이 안 되면 여기서 앱이 죽는다(기록 없이 콜백만 나가는 상태를 만들지 않으려고).
    app.state.engine = _connect_db(settings)

    # 카탈로그 — 설정 한 줄로 DB(배포)와 파일(로컬) 중 하나. 로드 실패해도 앱은 띄운다: 7.6이 503을 내고, /health가 active=false를 보이게.
    try:
        ## 상품DB연결
        app.state.catalog = (DbCatalogReader(app.state.engine, poll_ttl_s=settings.CATALOG_POLL_TTL_S) if settings.CATALOG_SOURCE == "db"
                             else FileCatalogReader(Path(settings.CATALOG_FILE)))
        version_id, products = app.state.catalog.active()
        log.info("카탈로그 로드 source=%s version=%s products=%d (%s)", settings.CATALOG_SOURCE, version_id, len(products),
                 f"ai_catalog poll_ttl_s={settings.CATALOG_POLL_TTL_S}" if settings.CATALOG_SOURCE == "db" else settings.CATALOG_FILE)
    except NoActiveCatalog as e:
        log.error("활성 카탈로그 없음: %s — 7.6은 503을 반환합니다", e)
        app.state.catalog = _NoCatalog(str(e))
    app.state.store = DbProfileRunStore(app.state.engine)
    app.state.recipient_store = DbRecipientProfileStore(app.state.engine)

    # 끊긴 실행 정리 — 프로세스가 죽으면 그 행은 RUNNING 인 채 남는다. 남겨 두면 중복 판정이 "이미 분석 중"으로
    # 읽어 Backend 재전송을 계속 건너뛴다. 다른 인스턴스가 **지금 돌리고 있는** 행까지 건드리지 않도록
    # RUNNING_STALE_S 보다 오래된 것만 FAILED 로 내린다(중복 판정의 기준과 같은 값).
    stale = app.state.store.recover_stale_runs(settings.RUNNING_STALE_S)
    if stale:
        log.warning("끊긴 실행 %d건을 FAILED 로 정리했습니다 (%d초 넘게 RUNNING) — Backend 가 다시 보내면 새로 분석합니다",
                    stale, settings.RUNNING_STALE_S)

    app.state.io_limiter = anyio.CapacityLimiter(settings.IO_THREADS)   # 접수 폴링·/health — 기본 스레드풀(토큰 40)과 분리, 실행 대기가 있어도 굶지 않는다
    app.state.supervisor = Supervisor(profiling_slots=settings.PROFILING_SLOTS, queue_max=settings.QUEUE_MAX)   # 3단계 §12.1 시작값 슬롯 1
    app.state.supervisor.start()
    app.state.backend = HttpBackendPort(settings.BACKEND_BASE_URL, settings.SERVICE_TOKEN, settings.CALLBACK_TIMEOUT_S)
    log.info("profiling 시작 backend=%s pool_size=%s", settings.BACKEND_BASE_URL, settings.POOL_SIZE)

    yield

    # 종료 — 신규 접수를 막고 큐를 버린 뒤 실행 중인 것만 SHUTDOWN_DRAIN_S 까지 기다린다(compose 유예 10초 안). 그 다음 콜백 클라이언트·DB 풀 정리.
    dropped = app.state.supervisor.stop(settings.SHUTDOWN_DRAIN_S)
    log.info("profiling 종료 dropped=%d", dropped)
    close = getattr(app.state.backend, "close", None)
    if callable(close):
        close()
    app.state.engine.dispose()


def _connect_db(settings: Settings) -> sa.Engine:
    """Engine 생성 + 연결 확인 + 마이그레이션 버전 확인. 실패하면 RuntimeError로 앱 시작을 막는다.

    pool_pre_ping: 풀에서 꺼낸 커넥션이 죽어 있으면(DB 재시작) 버리고 새로 연다 — 백그라운드 작업이 오래 뒤에 실행되므로 필요.
    """
    engine = sa.create_engine(settings.DATABASE_URL, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")     # 읽기 한 문장 — BEGIN/ROLLBACK 없이
            version = conn.execute(sa.text("select version_num from alembic_version")).scalar()
    except sa.exc.OperationalError as e:
        engine.dispose()
        raise RuntimeError(
            f"DB 연결 실패 ({settings.DATABASE_URL.split('@')[-1]}): {e.orig if hasattr(e, 'orig') else e}\n"
            "  → docker compose up -d && uv run alembic upgrade head"
        ) from e
    except sa.exc.ProgrammingError as e:    # alembic_version 테이블 없음 = 마이그레이션 안 됨
        engine.dispose()
        raise RuntimeError("DB는 있으나 마이그레이션이 적용되지 않음 → uv run alembic upgrade head") from e
    log.info("DB 연결 %s migration=%s", settings.DATABASE_URL.split("@")[-1], version)
    return engine


class _NoCatalog:
    """카탈로그 로드에 실패했을 때 app.state.catalog 자리에 두는 대역 — active()가 항상 NoActiveCatalog.
    CatalogReader 모양(active)만 맞추면 되므로 상속 없음."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def active(self):
        raise NoActiveCatalog(self.reason)


# ---------------------------------------------------------------------------
# 앱
# ---------------------------------------------------------------------------

app = FastAPI(title="profiling", version="0.1.0", lifespan=lifespan)
app.include_router(intake.router)


def _error_response(status: int, code: str, message: str, trace_id: str | None = None, *, retry_after: int | None = None) -> JSONResponse:
    """문서 1 §3 오류 봉투 {message, error: {code, traceId}}. 모든 오류 응답은 이 함수만 거친다.

    503 에는 `Retry-After` 를 붙인다 — 활성 카탈로그는 사람이 적재해야 돌아오므로 Backend 가 곧바로 다시 보내면
    의미 없는 요청만 쌓인다(그동안 sourceVersion 만 올라간다). 필드표 v1 §3.6 이 이 헤더를 전제로 쓰여 있다.
    retry_after 가 오면 그 값(예: 대기열 가득은 QUEUE_FULL_RETRY_AFTER_S), 없으면 RETRY_AFTER_S(카탈로그 없음).
    """
    body = ErrorResponse(message=message, error=ErrorBody(code=code, traceId=trace_id))
    headers = None
    if status == 503:
        headers = {"Retry-After": str(retry_after if retry_after is not None else get_settings().RETRY_AFTER_S)}
    return JSONResponse(status_code=status, content=body.model_dump(), headers=headers)


@app.exception_handler(RequestValidationError)
async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Pydantic 검증 실패 — FastAPI 기본은 422이지만 문서 1은 400 INVALID_REQUEST.
    첫 번째 오류의 위치·사유를 message에 넣어 Backend가 무엇을 고칠지 알 수 있게 한다."""
    first = exc.errors()[0] if exc.errors() else {}
    if first.get("type") == "json_invalid":                       # 본문이 JSON이 아님 — 필드 위치가 없다
        msg = "요청 본문이 JSON이 아닙니다."
    else:
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        msg = f"요청 형식이 올바르지 않습니다: {loc} — {first.get('msg', '')}".strip()
    log.warning("400 INVALID_REQUEST %s %s", request.url.path, msg)
    return _error_response(400, "INVALID_REQUEST", msg)


@app.exception_handler(StarletteHTTPException)
async def on_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """라우터가 던진 HTTPException(401·503 등)을 같은 봉투로. detail이 {code, message}면 그대로, 문자열이면 상태 코드로 code를 정한다."""
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        return _error_response(exc.status_code, exc.detail["code"], exc.detail.get("message", ""), retry_after=exc.detail.get("retryAfter"))
    default_code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 503: "SERVICE_UNAVAILABLE"}.get(exc.status_code, "INTERNAL_SERVER_ERROR")
    return _error_response(exc.status_code, default_code, str(exc.detail))


@app.exception_handler(Exception)
async def on_unhandled(request: Request, exc: Exception) -> JSONResponse:
    """잡히지 않은 예외 → 500 INTERNAL_SERVER_ERROR. Backend는 다음 디바운스 주기에 재시도한다(문서 1 §7.6)."""
    log.exception("500 %s", request.url.path)
    return _error_response(500, "INTERNAL_SERVER_ERROR", "서버 오류가 발생했습니다.")


@app.get("/health", tags=["ops"])
async def health(request: Request) -> dict:
    """살아 있는지 + 활성 카탈로그가 있는지. 카탈로그가 없어도 200 — 프로세스는 살아 있으므로(7.6은 503).

    async 라 기본 스레드풀 토큰을 쓰지 않는다. DB 문장 셋은 io_limiter(별도 스레드)에서 — 실행 대기가 쌓여도 /health 는 답한다(09-28).
    """
    state = request.app.state
    return await anyio.to_thread.run_sync(_health_sync, state, limiter=state.io_limiter)


def _health_sync(state) -> dict:
    try:
        version_id, products = state.catalog.active()
        catalog = {"active": True, "version": str(version_id), "products": len(products)}
        if getattr(state.catalog, "provisional_ids", False):
            # Backend 번호가 아직 없어 임시 번호로 도는 상태 — 배포는 되지만 7.7로 내보낸 번호는 Backend에 없다
            catalog["provisional_ids"] = True
    except NoActiveCatalog as e:
        catalog = {"active": False, "reason": str(e)}
    return {"status": "ok", "catalog": catalog, "store": _store_health(state), "supervisor": state.supervisor.stats()}


def _store_health(state) -> dict:
    """저장소 상태 — 지금 연결되는지와 마이그레이션 버전. 확인 자체가 실패해도 /health는 200(프로세스는 살아 있으므로)."""
    engine = state.engine
    try:
        with engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")     # 읽기 한 문장 — BEGIN/ROLLBACK 없이
            version = conn.execute(sa.text("select version_num from alembic_version")).scalar()
        # 결과는 만들었는데 Backend 에 전달하지 못한 행. 늘어나면 콜백 경로에 문제가 있다는 뜻이라 운영이 바로 봐야 한다.
        return {"backend": "db", "connected": True, "migration": version,
                "undelivered": state.store.undelivered_count()}
    except sa.exc.SQLAlchemyError as e:      # 연결 끊김·테이블 없음 등 DB 쪽 오류만 — 그 외는 500으로 드러나야 한다
        return {"backend": "db", "connected": False, "reason": type(e).__name__}
