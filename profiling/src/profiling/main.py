"""앱 조립 — 어떤 adapter 구현을 끼울지 정하는 유일한 곳.

실행:  uv run uvicorn profiling.main:app --port 8000 --reload
확인:  curl localhost:8000/health

여기서만 adapters의 구체 클래스를 import한다. 라우터(transport/)와 업무(profile/)는 app.state에 든 객체를 ports의 모양으로만 쓴다.

app.state에 두는 것 (lifespan에서 1회 생성):
  catalog          CatalogReader        FileCatalogReader (팀원 카탈로그 DB 전까지 파일)
  store            ProfileRunStore      STORE=db → DbProfileRunStore(profile_runs) · memory → MemoryProfileRunStore
  recipient_store  RecipientProfileStore  STORE=db → DbRecipientProfileStore(recipient_profiles) · memory → None(저장 안 함)
  backend          BackendPort          HttpBackendPort
  supervisor       Supervisor
  engine           sqlalchemy Engine    STORE=db 일 때만. 종료 시 dispose
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from profiling.adapters.backend_port_http import HttpBackendPort
from profiling.adapters.catalog_reader_file import FileCatalogReader
from profiling.adapters.profile_run_store_db import DbProfileRunStore
from profiling.adapters.profile_run_store_memory import MemoryProfileRunStore
from profiling.adapters.recipient_profile_store_db import DbRecipientProfileStore
from profiling.config.settings import Settings, get_settings
from profiling.profile.ports import NoActiveCatalog
from profiling.runtime.supervisor import Supervisor
from profiling.transport import profile_intake
from profiling.transport.schemas import ErrorBody, ErrorResponse

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 시작·종료 — adapter 생성은 여기서 1회. 요청마다 만들지 않는다.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # 카탈로그 — 오늘은 파일. 로드 실패해도 앱은 띄운다: 7.6이 503을 내고, /health가 catalog=false를 보이게.
    try:
        ## 상품DB연결
        app.state.catalog = FileCatalogReader(Path(settings.CATALOG_FILE))
        version_id, products = app.state.catalog.active()
        log.info("카탈로그 로드 version=%s products=%d (%s)", version_id, len(products), settings.CATALOG_FILE)
    except NoActiveCatalog as e:
        log.error("활성 카탈로그 없음: %s — 7.6은 503을 반환합니다", e)
        app.state.catalog = _NoCatalog(str(e))

    # 저장소 — 기본은 DB. 연결이 안 되면 여기서 앱이 죽는다(조용히 메모리로 내려가면 콜백은 나가는데 기록이 없는 상태가 되므로).
    app.state.engine = None
    if settings.STORE == "db":
        app.state.engine = _connect_db(settings)
        app.state.store = DbProfileRunStore(app.state.engine)
        app.state.recipient_store = DbRecipientProfileStore(app.state.engine)
    else:
        log.warning("STORE=memory — 실행 기록은 프로세스 메모리, 수신자 프로필은 저장하지 않음 (시험용)")
        app.state.store = MemoryProfileRunStore()
        app.state.recipient_store = None

    app.state.supervisor = Supervisor(profiling_slots=settings.PROFILING_SLOTS)   # 3단계 §12.1 시작값 1
    app.state.backend = HttpBackendPort(settings.BACKEND_BASE_URL, settings.SERVICE_TOKEN, settings.CALLBACK_TIMEOUT_S)
    log.info("profiling 시작 backend=%s pool_size=%s store=%s", settings.BACKEND_BASE_URL, settings.POOL_SIZE, settings.STORE)

    yield

    # 종료 — HttpBackendPort가 httpx.Client를 들고 있으면 닫는다. 없어도 조용히 넘어간다. DB 커넥션 풀도 정리.
    close = getattr(app.state.backend, "close", None)
    if callable(close):
        close()
    if app.state.engine is not None:
        app.state.engine.dispose()


def _connect_db(settings: Settings) -> sa.Engine:
    """Engine 생성 + 연결 확인 + 마이그레이션 버전 확인. 실패하면 RuntimeError로 앱 시작을 막는다.

    pool_pre_ping: 풀에서 꺼낸 커넥션이 죽어 있으면(DB 재시작) 버리고 새로 연다 — 백그라운드 작업이 오래 뒤에 실행되므로 필요.
    """
    engine = sa.create_engine(settings.DATABASE_URL, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as conn:
            version = conn.execute(sa.text("select version_num from alembic_version")).scalar()
    except sa.exc.OperationalError as e:
        engine.dispose()
        raise RuntimeError(
            f"DB 연결 실패 ({settings.DATABASE_URL.split('@')[-1]}): {e.orig if hasattr(e, 'orig') else e}\n"
            "  → docker compose up -d && uv run alembic upgrade head   (또는 DB 없이 띄우려면 PROFILING_STORE=memory)"
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
app.include_router(profile_intake.router)


def _error_response(status: int, code: str, message: str, trace_id: str | None = None) -> JSONResponse:
    """문서 1 §3 오류 봉투 {message, error: {code, traceId}}. 모든 오류 응답은 이 함수만 거친다."""
    body = ErrorResponse(message=message, error=ErrorBody(code=code, traceId=trace_id))
    return JSONResponse(status_code=status, content=body.model_dump())


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
        return _error_response(exc.status_code, exc.detail["code"], exc.detail.get("message", ""))
    default_code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 503: "SERVICE_UNAVAILABLE"}.get(exc.status_code, "INTERNAL_SERVER_ERROR")
    return _error_response(exc.status_code, default_code, str(exc.detail))


@app.exception_handler(Exception)
async def on_unhandled(request: Request, exc: Exception) -> JSONResponse:
    """잡히지 않은 예외 → 500 INTERNAL_SERVER_ERROR. Backend는 다음 디바운스 주기에 재시도한다(문서 1 §7.6)."""
    log.exception("500 %s", request.url.path)
    return _error_response(500, "INTERNAL_SERVER_ERROR", "서버 오류가 발생했습니다.")


@app.get("/health", tags=["ops"])
def health(request: Request) -> dict:
    """살아 있는지 + 활성 카탈로그가 있는지. 카탈로그가 없어도 200 — 프로세스는 살아 있으므로(7.6은 503)."""
    try:
        version_id, products = request.app.state.catalog.active()
        catalog = {"active": True, "version": str(version_id), "products": len(products)}
    except NoActiveCatalog as e:
        catalog = {"active": False, "reason": str(e)}
    return {"status": "ok", "catalog": catalog, "store": _store_health(request), "supervisor": request.app.state.supervisor.stats()}


def _store_health(request: Request) -> dict:
    """저장소 상태 — db면 지금 연결되는지와 마이그레이션 버전까지. 확인 자체가 실패해도 /health는 200(프로세스는 살아 있으므로)."""
    engine = request.app.state.engine
    if engine is None:
        return {"backend": "memory", "connected": True}
    try:
        with engine.connect() as conn:
            version = conn.execute(sa.text("select version_num from alembic_version")).scalar()
        return {"backend": "db", "connected": True, "migration": version}
    except sa.exc.SQLAlchemyError as e:      # 연결 끊김·테이블 없음 등 DB 쪽 오류만 — 그 외는 500으로 드러나야 한다
        return {"backend": "db", "connected": False, "reason": type(e).__name__}
