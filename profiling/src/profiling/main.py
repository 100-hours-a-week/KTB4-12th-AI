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

from profiling import intake, status
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

# 이 코드가 기대하는 DB 마이그레이션 판 — alembic head 와 같아야 한다(tests/unit/test_ops_endpoints.py 가 지킨다).
# 새 마이그레이션을 더하면 여기도 올린다. /readyz 와 STRICT_STARTUP 이 이 값과 DB 의 alembic_version 을 비교한다(10-15).
EXPECTED_MIGRATION = "0008"
_DEFAULT_SECRETS = ("dev-token",)
_DEFAULT_DB_PASSWORDS = (":ai_password@", ":ai_app_password@")
_DEFAULT_BACKEND = "http://localhost:8081"


def strict_problems(settings: Settings) -> list[str]:
    """STRICT_STARTUP 에서 기동을 막을 문제 목록(빈 목록이면 통과). 값 자체는 담지 않는다 — 어느 설정이 문제인지만."""
    problems = []
    if not settings.SERVICE_TOKEN.strip() or settings.SERVICE_TOKEN in _DEFAULT_SECRETS:
        problems.append("PROFILING_SERVICE_TOKEN 이 비었거나 공개 기본값")
    if any(p in settings.DATABASE_URL for p in _DEFAULT_DB_PASSWORDS):
        problems.append("PROFILING_DATABASE_URL 의 비밀번호가 공개 기본값")
    if settings.BACKEND_BASE_URL.rstrip("/") == _DEFAULT_BACKEND:
        problems.append("PROFILING_BACKEND_BASE_URL 이 로컬 가짜 BE 기본값")
    if settings.CATALOG_SOURCE != "db":
        problems.append("PROFILING_CATALOG_SOURCE 가 db 가 아님(서버 이미지에는 카탈로그 파일이 없다)")
    return problems


# ---------------------------------------------------------------------------
# 시작·종료 — adapter 생성은 여기서 1회. 요청마다 만들지 않는다.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if settings.STRICT_STARTUP and (problems := strict_problems(settings)):
        raise RuntimeError("STRICT_STARTUP 기동 거부 — " + " · ".join(problems))

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
        if settings.CATALOG_SOURCE == "db":
            # DB 카탈로그는 1초마다 활성 버전을 다시 묻는다 — 기동 뒤에 적재해도 재시작 없이 살아난다(10-15: 대역으로 바꾸면 영영 503 이었다)
            log.error("활성 카탈로그 없음: %s — 적재될 때까지 7.6 은 503 · /readyz 503(적재되면 재시작 없이 회복)", e)
        else:
            log.error("활성 카탈로그 없음: %s — 7.6은 503을 반환합니다", e)
            app.state.catalog = _NoCatalog(str(e))
    app.state.store = DbProfileRunStore(app.state.engine)

    # 태그 추출기 · 임베딩 — 설정으로 켤 때만(기본 꺼짐). 만들다 실패하면 끈 채로 뜬다: 태그 없이 D 가 돈다(요청은 실패하지 않음)
    app.state.tagger = app.state.embedding = app.state.tag_spec = None
    app.state.embedding_status = {"enabled": False, "reason": "not_configured"}
    tagger_reason: str | None = None
    if settings.TAGGER != "none":
        from profiling import tagging

        # 태그 규격(축 · 프롬프트 한 묶음) — 기동 때 한 번 읽어 요청마다 넘긴다. 규격 · 프롬프트 축이 어긋나면 여기서 ValueError → 서버가 뜨지 않음
        app.state.tag_spec = tagging.load_spec()
        sp = app.state.tag_spec
        log.info("태그 규격 %s 축=%s 판=%s 파일=%s", sp.name, "·".join(sp.axes), sp.version, sp.prompt_file)
    if settings.TAGGER == "ollama":
        from profiling.llm import OllamaTagExtractor
        app.state.tagger = OllamaTagExtractor(settings.OLLAMA_URL, settings.TAGGER_MODEL, timeout_s=settings.TAGGER_TIMEOUT_S)
        app.state.tagger.warm_up()
        log.info("태그 추출기 ollama model=%s url=%s", settings.TAGGER_MODEL, settings.OLLAMA_URL)
    elif settings.TAGGER == "openrouter":
        if settings.OPENROUTER_API_KEY is None or not settings.OPENROUTER_API_KEY.get_secret_value().strip():
            log.error("TAGGER=openrouter 인데 PROFILING_OPENROUTER_API_KEY 가 없음 — 태그 추출기 끈 채로 진행")
            tagger_reason = "no_api_key"
        else:
            from profiling.llm import OpenRouterTagExtractor
            app.state.tagger = OpenRouterTagExtractor(settings.OPENROUTER_URL, settings.OPENROUTER_API_KEY.get_secret_value(),
                                                      settings.TAGGER_MODEL, timeout_s=settings.TAGGER_TIMEOUT_S)
            log.info("태그 추출기 openrouter model=%s url=%s", settings.TAGGER_MODEL, settings.OPENROUTER_URL)   # 키는 남기지 않음
    if settings.EMBEDDING_DIR:
        try:
            from profiling.embedding import ProductSearchEmbedding
            app.state.embedding = ProductSearchEmbedding(Path(settings.EMBEDDING_DIR), app.state.engine)
            emb = app.state.embedding
            matched, total = getattr(emb, "matched", None), getattr(emb, "total", None)      # 매칭 수는 임베딩 수정(fix 브랜치) 뒤에 생긴다
            app.state.embedding_status = {"enabled": True, "version": emb.version, "matched": matched, "total": total}
            log.info("임베딩 %s (%s) 상품 매칭 %s/%s", emb.version, settings.EMBEDDING_DIR, matched, total)
        except Exception as e:
            log.exception("임베딩을 켜지 못함 — 끈 채로 진행(태그가 있어도 임베딩 단계 없이 D)")
            app.state.embedding_status = {"enabled": False, "reason": type(e).__name__}
    status.LLM.reset()
    status.LLM.configure(requested=settings.TAGGER, enabled=app.state.tagger is not None,
                         model=settings.TAGGER_MODEL if settings.TAGGER != "none" else None, reason=tagger_reason)
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
    log.info("profiling 시작 backend=%s pool_size=%s dislike_mode=%s pool_strategy=%s", settings.BACKEND_BASE_URL, settings.POOL_SIZE,
             settings.DISLIKE_MODE, settings.POOL_STRATEGY)

    yield

    # 종료 — 신규 접수를 막고 큐를 버린 뒤 실행 중인 것만 SHUTDOWN_DRAIN_S 까지 기다린다(compose 유예 10초 안). 그 다음 콜백 클라이언트·DB 풀 정리.
    dropped = app.state.supervisor.stop(settings.SHUTDOWN_DRAIN_S)
    log.info("profiling 종료 dropped=%d", dropped)
    for part in (app.state.backend, getattr(app.state, "tagger", None), getattr(app.state, "embedding", None)):
        close = getattr(part, "close", None)
        if callable(close):
            close()
    app.state.engine.dispose()


def db_pool_size(settings: Settings) -> int:
    """상시 커넥션 수 — 설정이 없으면 슬롯 수 + IO_THREADS(슬롯마다 잠금 커넥션 1개를 분석 내내 쥐고, 접수 폴링 · /health 가 그만큼 더 쓴다)."""
    return settings.DB_POOL_SIZE or settings.PROFILING_SLOTS + settings.IO_THREADS


def db_session_options(settings: Settings) -> str:
    """세션마다 거는 시간 상한 — libpq 접속 옵션(-c). PgBouncer(트랜잭션 모드)는 접속 옵션을 받지 않으니 그때는 역할(ALTER ROLE … SET)로 옮긴다."""
    return " ".join(f"-c {name}={ms}" for name, ms in (
        ("statement_timeout", settings.DB_STATEMENT_TIMEOUT_MS),
        ("lock_timeout", settings.DB_LOCK_TIMEOUT_MS),
        ("idle_in_transaction_session_timeout", settings.DB_IDLE_IN_TX_TIMEOUT_MS)))


def _connect_db(settings: Settings) -> sa.Engine:
    """Engine 생성 + 연결 확인 + 마이그레이션 버전 · 접속 계정 확인. 실패하면 RuntimeError로 앱 시작을 막는다.

    pool_pre_ping: 풀에서 꺼낸 커넥션이 죽어 있으면(DB 재시작) 버리고 새로 연다 — 백그라운드 작업이 오래 뒤에 실행되므로 필요.
    풀 크기 · 시간 상한 · 슈퍼유저 거부는 10-13 DB 검토(설정 DB_*).
    """
    pool_size, need = db_pool_size(settings), settings.PROFILING_SLOTS + settings.IO_THREADS
    if pool_size + settings.DB_MAX_OVERFLOW < need:
        raise RuntimeError(f"DB 커넥션 풀이 작다: pool_size {pool_size} + max_overflow {settings.DB_MAX_OVERFLOW} < "
                           f"슬롯 {settings.PROFILING_SLOTS} + IO_THREADS {settings.IO_THREADS} — 슬롯이 커넥션을 기다리다 실패한다. "
                           "PROFILING_DB_POOL_SIZE 를 비우거나(자동) 늘린다")
    engine = sa.create_engine(
        settings.DATABASE_URL, pool_pre_ping=True, future=True,
        pool_size=pool_size, max_overflow=settings.DB_MAX_OVERFLOW, pool_timeout=settings.DB_POOL_TIMEOUT_S,
        connect_args={"connect_timeout": settings.DB_CONNECT_TIMEOUT_S, "options": db_session_options(settings)},
    )
    try:
        with engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")     # 읽기 한 문장 — BEGIN/ROLLBACK 없이
            version, user, superuser = conn.execute(sa.text(
                "select (select version_num from alembic_version), current_user, "
                "(select rolsuper from pg_roles where rolname = current_user)")).one()
    except sa.exc.OperationalError as e:
        engine.dispose()
        raise RuntimeError(
            f"DB 연결 실패 ({settings.DATABASE_URL.split('@')[-1]}): {e.orig if hasattr(e, 'orig') else e}\n"
            "  → docker compose up -d && uv run alembic upgrade head"
        ) from e
    except sa.exc.ProgrammingError as e:    # alembic_version 없음(마이그레이션 안 됨) · 읽을 권한 없음(ai_app 인데 0007 이 안 됨)
        engine.dispose()
        raise RuntimeError(f"DB는 있으나 마이그레이션 장부를 못 읽음({type(e.orig).__name__}) → uv run alembic upgrade head") from e
    if version != EXPECTED_MIGRATION:
        msg = f"DB 마이그레이션 판 {version} ≠ 코드가 기대하는 {EXPECTED_MIGRATION} — 마이그레이션(alembic upgrade head)을 먼저 돌린다"
        if settings.STRICT_STARTUP:
            engine.dispose()
            raise RuntimeError(msg)
        log.warning("%s (로컬 — STRICT_STARTUP 이 아니라 계속 뜬다. /readyz 는 503)", msg)
    if superuser:
        if settings.DB_REFUSE_SUPERUSER:
            engine.dispose()
            raise RuntimeError(f"앱이 슈퍼유저 계정({user})으로 접속했다 — 앱 전용 계정 ai_app(0007)으로 PROFILING_DATABASE_URL 을 바꾼다")
        log.warning("앱이 슈퍼유저 계정(%s)으로 접속 — 로컬 전용. 운영 · 개발 서버는 ai_app + PROFILING_DB_REFUSE_SUPERUSER=true", user)
    log.info("DB 연결 %s migration=%s user=%s pool=%d+%d timeout=%.0fs statement=%dms lock=%dms",
             settings.DATABASE_URL.split("@")[-1], version, user, pool_size, settings.DB_MAX_OVERFLOW, settings.DB_POOL_TIMEOUT_S,
             settings.DB_STATEMENT_TIMEOUT_MS, settings.DB_LOCK_TIMEOUT_MS)
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

    503 에는 `Retry-After` 를 붙인다. **Backend 는 이 헤더를 읽지 않는다**(통합 수정점 v0.7, 10-08 실측) — 503 을 재시도 가능
    실패로 보고 틱을 끝내며 디바운스를 1회 재시작한다(두 번째면 접음). 헤더는 운영자·다른 호출자를 위한 표준 힌트로 남긴다.
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
    """잡히지 않은 예외 → 500 INTERNAL_SERVER_ERROR. Backend 는 재시도 가능 실패로 보고 틱을 끝내며 디바운스를 1회 재시작한다
    (통합 수정점 v0.7 ②③, 10-08 실측 — 위키 7.6 의 "다음 디바운스 주기에 재시도"는 옛 문구)."""
    log.exception("500 %s", request.url.path)
    return _error_response(500, "INTERNAL_SERVER_ERROR", "서버 오류가 발생했습니다.")


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    """**Liveness**(ECS) — 프로세스가 살아 있으면 늘 200. DB · 카탈로그 · 외부 LLM 을 보지 않는다(그것들이 죽어도 재시작으로 안 고쳐진다).
    검색 · 챗봇이 이미 쓰는 이름을 따랐다(10-15 프로파일링 담당 선택). BE 핑은 /health — 그대로 둔다."""
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz(request: Request) -> JSONResponse:
    """**Readiness**(ECS) — 7.6 을 받아 처리할 준비가 됐나. 활성 카탈로그 · DB 연결 · 마이그레이션 판(EXPECTED_MIGRATION)이 다 맞으면 200,
    하나라도 아니면 503. **외부 LLM 은 보지 않는다** — LLM 이 죽어도 태그 없이 30개를 보내므로 트래픽에서 빼면 안 된다(10-15 프로파일링 담당 선택).
    LLM 상태는 /health 의 `llm` 칸."""
    state = request.app.state
    checks = await anyio.to_thread.run_sync(_ready_checks, state, limiter=state.io_limiter)
    ready = all(checks.values())
    return JSONResponse(status_code=200 if ready else 503, content={"ready": ready, "checks": checks})


def _ready_checks(state) -> dict[str, bool]:
    try:
        state.catalog.active()
        catalog = True
    except NoActiveCatalog:
        catalog = False
    try:
        with state.engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            version = conn.execute(sa.text("select version_num from alembic_version")).scalar()
        db, migration = True, version == EXPECTED_MIGRATION
    except sa.exc.SQLAlchemyError:
        db = migration = False
    return {"catalog": catalog, "db": db, "migration": migration}


@app.get("/health", tags=["ops"])
async def health(request: Request) -> dict:
    """살아 있는지 + 활성 카탈로그가 있는지. 카탈로그가 없어도 200 — 프로세스는 살아 있으므로(7.6은 503).

    **BE 핑 계약(통합 수정점 v0.7 ①, 10-08 실측):** Backend 는 틱마다 토큰 없이 `GET /health`(2초 타임아웃)를 부르고,
    200 이면서 `catalog.active` 와 `store.connected` 가 **둘 다 true** 일 때만 7.6 을 보낸다 — 아니면 번호를 올리지 않고 틱을 건너뛴다.
    이 두 키의 이름·뜻·타입(bool)을 바꾸면 Backend 가 모든 틱을 건너뛴다. tests/unit/test_health_contract.py 가 지킨다.

    async 라 기본 스레드풀 토큰을 쓰지 않는다. DB 문장 셋은 io_limiter(별도 스레드)에서 — 실행 대기가 쌓여도 /health 는 답한다(09-28).
    """
    state = request.app.state
    return await anyio.to_thread.run_sync(_health_sync, state, limiter=state.io_limiter)


def _health_sync(state) -> dict:
    try:
        version_id, products = state.catalog.active()
        catalog = {"active": True, "version": str(version_id), "products": len(products)}
    except NoActiveCatalog as e:
        catalog = {"active": False, "reason": str(e)}
    return {"status": "ok", "catalog": catalog, "store": _store_health(state), "supervisor": state.supervisor.stats(),
            "llm": status.LLM.snapshot(), "embedding": getattr(state, "embedding_status", {"enabled": False})}


def _store_health(state) -> dict:
    """저장소 상태 — 지금 연결되는지와 마이그레이션 버전. 확인 자체가 실패해도 /health는 200(프로세스는 살아 있으므로)."""
    engine = state.engine
    try:
        with engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")     # 읽기 한 문장 — BEGIN/ROLLBACK 없이
            version = conn.execute(sa.text("select version_num from alembic_version")).scalar()
        # 결과는 만들었는데 Backend 에 전달하지 못한 행. 늘어나면 콜백 경로에 문제가 있다는 뜻이라 운영이 바로 봐야 한다.
        return {"backend": "db", "connected": True, "migration": version, "migration_expected": EXPECTED_MIGRATION,
                "undelivered": state.store.undelivered_count()}
    except sa.exc.SQLAlchemyError as e:      # 연결 끊김·테이블 없음 등 DB 쪽 오류만 — 그 외는 500으로 드러나야 한다
        return {"backend": "db", "connected": False, "reason": type(e).__name__}
