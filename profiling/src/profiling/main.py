"""앱 조립 — 어떤 adapter 구현을 끼울지 정하는 유일한 곳.

실행:  uv run uvicorn profiling.main:app --port 8000 --reload
확인:  curl localhost:8000/health

여기서만 adapters의 구체 클래스를 import한다. 라우터(api/)와 업무(profile/)는 app.state에 든 객체를 ports의 모양으로만 쓴다.
내일 DB로 바꿀 때는 lifespan의 세 줄(FileCatalogReader·MemoryProfileRunStore·HttpBackendPort)만 바뀐다.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from profiling.adapters.backend_port_http import HttpBackendPort
from profiling.adapters.catalog_reader_file import FileCatalogReader
from profiling.adapters.profile_run_store_memory import MemoryProfileRunStore
from profiling.config.settings import get_settings
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

    app.state.store = MemoryProfileRunStore()
    app.state.supervisor = Supervisor(profiling_slots=settings.PROFILING_SLOTS)   # 3단계 §12.1 시작값 1
    app.state.backend = HttpBackendPort(settings.BACKEND_BASE_URL, settings.SERVICE_TOKEN, settings.CALLBACK_TIMEOUT_S)
    log.info("profiling 시작 backend=%s pool_size=%s", settings.BACKEND_BASE_URL, settings.POOL_SIZE)

    yield

    # 종료 — HttpBackendPort가 httpx.Client를 들고 있으면 닫는다. 없어도 조용히 넘어간다.
    close = getattr(app.state.backend, "close", None)
    if callable(close):
        close()


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
    return {"status": "ok", "catalog": catalog, "supervisor": request.app.state.supervisor.stats()}
