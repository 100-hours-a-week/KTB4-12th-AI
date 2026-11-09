"""앱 조립 — 어떤 adapter 구현을 끼울지 정하는 유일한 곳.

실행:  uv run uvicorn profiling.main:app --port 8000 --reload
확인:  curl localhost:8000/health

여기서만 구체 클래스(DbCatalogReader·FileCatalogReader · Db*Store)를 import한다. MQ 처리기(mq/)와 업무(pipeline.py)는 app.state에 든 객체를 ports의 모양으로만 쓴다.
BE ↔ AI 프로파일링 요청 · 결과는 RabbitMQ 뿐이다 — HTTP 7.6 접수 · 7.7 콜백 · 작업 큐(Supervisor)는 11-08 MQ 로 대체하며 지웠다.

app.state에 두는 것 (lifespan에서 1회 생성):
  catalog          CatalogReader        DbCatalogReader(ai_catalog) 또는 FileCatalogReader — CATALOG_SOURCE 설정
  store            ProfileRunStore      DbProfileRunStore (ai_profile.profile_runs)
  recipient_store  RecipientProfileStore  DbRecipientProfileStore (ai_profile.recipient_profiles)
  io_limiter       anyio.CapacityLimiter  /health · /readyz · 조회 API 의 짧은 DB 문장용(기본 스레드풀과 분리)
  engine           sqlalchemy Engine    시작 시 연결 확인, 종료 시 dispose
  mq               MqRuntime | None     RabbitMQ 요청 소비자(PROFILING_MQ_ENABLED 일 때만) — start() 하고, 종료 때 DB 풀보다 먼저 stop()
                                         mq_defer_start 가 참이면 start() 를 부른 쪽(통합 런타임)에 맡긴다
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

from profiling import status
from profiling.catalog import DbCatalogReader, FileCatalogReader
from profiling.ports import NoActiveCatalog
from profiling.profile_read import router as profile_read_router
from profiling.schemas import ErrorBody, ErrorResponse
from profiling.settings import Settings, get_settings
from profiling.stores import (
    DbProfileRunStore,
    DbRecipientProfileStore,
)

log = logging.getLogger(__name__)


class _ASGIExceptionLogFilter(logging.Filter):
    """Starlette가 500 전송 뒤 재전파한 예외도 Uvicorn에서 종류만 기록한다. 다른 서버 진단은 유지한다."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.error" or not isinstance(record.msg, str) or record.msg.rstrip() != "Exception in ASGI application":
            return True
        error_type = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else "UnknownException"
        record.msg = "Exception in ASGI application: %s"
        record.args = (error_type,)
        record.exc_info = None
        record.exc_text = None  # 앞선 formatter가 만든 traceback 캐시도 제거한다.
        record.stack_info = None
        record.message = record.getMessage()
        return True


_asgi_error_log = logging.getLogger("uvicorn.error")
if not any(isinstance(f, _ASGIExceptionLogFilter) for f in _asgi_error_log.filters):
    _asgi_error_log.addFilter(_ASGIExceptionLogFilter())

# 이 코드가 기대하는 DB 마이그레이션 판 — alembic head 와 같아야 한다(tests/unit/test_ops_endpoints.py 가 지킨다).
# 새 마이그레이션을 더하면 여기도 올린다. /readyz 와 STRICT_STARTUP 이 이 값과 DB 의 alembic_version 을 비교한다(10-15).
# 비교는 "DB 판 ≥ 기대 판"이다 — 롤링 배포는 마이그레이션을 먼저 돌리므로 그동안 옛 Task 는 한 판 높은 DB 위에서 돈다.
# 같음으로 비교하면 마이그레이션 순간 옛 Task 가 모두 준비 안 됨(503)이 되어 AI 가 빈다. 그래서 마이그레이션은 옛 코드도 도는
# 모양(열 추가 먼저 → 코드 → 삭제는 다음 판)으로 쓴다. 판 이름은 4자리 숫자라 문자열 비교가 곧 순서다.
EXPECTED_MIGRATION = "0012"


def migration_ok(version: str | None) -> bool:
    """DB 판이 이 코드가 도는 데 충분한가 — 기대 판 이상이면 참. 판을 못 읽었거나 낮으면 거짓."""
    return bool(version) and len(version) == len(EXPECTED_MIGRATION) and version >= EXPECTED_MIGRATION
_DEFAULT_SECRETS = ("dev-token",)
_DEFAULT_DB_PASSWORDS = (":ai_password@", ":ai_app_password@")
_PUBLIC_DB_USERS = ("ai_user", "ai_app")      # 로컬 compose 의 공개 기본 계정 이름 — 서버는 비공개 이름(PROFILING_APP_DB_ROLE)


def _require_db_url(settings: Settings) -> str:
    """DB 주소는 코드에 기본값이 없다(10-15) — 없으면 바로 알려 주고 멈춘다."""
    if not settings.DATABASE_URL.strip():
        raise RuntimeError("PROFILING_DATABASE_URL 이 없다 — profiling/.env 또는 환경변수로 넣는다(값은 담당에게 받는 로컬 설정 템플릿)")
    return settings.DATABASE_URL


def strict_problems(settings: Settings, *, managed_auth=False) -> list[str]:
    """STRICT_STARTUP 에서 기동을 막을 문제 목록(빈 목록이면 통과). 값 자체는 담지 않는다 — 어느 설정이 문제인지만."""
    problems = []
    if not managed_auth and (not settings.SERVICE_TOKEN.strip() or settings.SERVICE_TOKEN in _DEFAULT_SECRETS):
        problems.append("PROFILING_SERVICE_TOKEN 이 비었거나 공개 기본값")
    try:
        db_user = sa.engine.make_url(settings.DATABASE_URL).username
    except sa.exc.ArgumentError:
        db_user = None
    if db_user in _PUBLIC_DB_USERS:
        problems.append("PROFILING_DATABASE_URL 의 계정 이름이 공개 기본값 — 서버는 비공개 앱 계정(PROFILING_APP_DB_ROLE)")
    if any(p in settings.DATABASE_URL for p in _DEFAULT_DB_PASSWORDS):
        problems.append("PROFILING_DATABASE_URL 의 비밀번호가 공개 기본값")
    if settings.CATALOG_SOURCE not in ("db", "search_http"):
        problems.append("PROFILING_CATALOG_SOURCE 가 db 가 아님(서버 이미지에는 카탈로그 파일이 없다)")
    if settings.CATALOG_SOURCE == "search_http" and not settings.SEARCH_BASE_URL:
        problems.append("PROFILING_SEARCH_BASE_URL required for shared catalog")
    return problems


# ---------------------------------------------------------------------------
# 시작·종료 — adapter 생성은 여기서 1회. 요청마다 만들지 않는다.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if settings.STRICT_STARTUP and (problems := strict_problems(settings, managed_auth=callable(getattr(app.state, "be_token_provider", None)))):
        raise RuntimeError("STRICT_STARTUP 기동 거부 — " + " · ".join(problems))

    # 저장소 — PostgreSQL 하나뿐. 연결이 안 되면 여기서 앱이 죽는다(기록 없이 결과만 나가는 상태를 만들지 않으려고).
    app.state.engine = _connect_db(settings)

    # 카탈로그 — 설정 한 줄로 DB(배포)와 파일(로컬) 중 하나. 로드 실패해도 앱은 띄운다: /readyz 가 503 이라 MQ 는 소비를 멈추고, /health 가 active=false 를 보이게.
    try:
        ## 상품DB연결
        if settings.CATALOG_SOURCE == "search_http":
            from profiling.search_api import SearchCatalog
            app.state.catalog = SearchCatalog(settings.SEARCH_BASE_URL,
                settings.SEARCH_TOKEN.get_secret_value() if settings.SEARCH_TOKEN else settings.SERVICE_TOKEN)
        else:
            app.state.catalog = (DbCatalogReader(app.state.engine, poll_ttl_s=settings.CATALOG_POLL_TTL_S) if settings.CATALOG_SOURCE == "db"
                                 else FileCatalogReader(Path(settings.CATALOG_FILE)))
        version_id, products = app.state.catalog.active()
        log.info("카탈로그 로드 source=%s version=%s products=%d (%s)", settings.CATALOG_SOURCE, version_id, len(products),
                 f"ai_catalog poll_ttl_s={settings.CATALOG_POLL_TTL_S}" if settings.CATALOG_SOURCE == "db" else settings.CATALOG_FILE)
    except NoActiveCatalog as e:
        if settings.CATALOG_SOURCE == "search_http":
            # A shared generation must never degrade into local/no-vector ranking.
            raise
        if settings.CATALOG_SOURCE == "db":
            # DB 카탈로그는 1초마다 활성 버전을 다시 묻는다 — 기동 뒤에 적재해도 재시작 없이 살아난다(10-15: 대역으로 바꾸면 영영 503 이었다)
            log.error("활성 카탈로그 없음: %s — 적재될 때까지 MQ 소비 멈춤 · /readyz 503(적재되면 재시작 없이 회복)", e)
        else:
            log.error("활성 카탈로그 없음: %s — MQ 소비 멈춤 · /readyz 503", e)
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
    stub_on = settings.TAGGER == "openrouter" and settings.TAGGER_STUB
    if settings.TAGGER_STUB and not stub_on:
        log.warning("TAGGER_STUB=true 는 TAGGER=openrouter 일 때만 켜진다 — 지금 TAGGER=%s, 대역 안 씀", settings.TAGGER)
    if settings.TAGGER == "ollama":
        from profiling.llm import OllamaTagExtractor
        app.state.tagger = OllamaTagExtractor(settings.OLLAMA_URL, settings.tagger_model, timeout_s=settings.TAGGER_TIMEOUT_S)
        app.state.tagger.warm_up()
        log.info("태그 추출기 ollama model=%s url=%s", settings.tagger_model, settings.OLLAMA_URL)
    elif settings.TAGGER == "openrouter" and settings.TAGGER_STUB:
        # 성능 측정 전용 대역(llm_stub.py) — 전송만 바꾼다. 키를 쓰지 않고(빈 값) 바깥 연결이 없어 비용 0. 설정이 모순이면 ValueError → 기동 실패
        from profiling import llm_stub
        from profiling.llm import OpenRouterTagExtractor
        transport = llm_stub.from_settings(settings)
        app.state.tagger = OpenRouterTagExtractor(settings.OPENROUTER_URL, "", llm_stub.STUB_MODEL,
                                                  timeout_s=settings.TAGGER_TIMEOUT_S, transport=transport)
        log.warning("태그 추출기 대역(TAGGER_STUB) — 모델 호출 없음 · 고정 답 · %s · v3 엔진 안 켬", transport.delay.describe())
    elif settings.TAGGER == "openrouter":
        if not getattr(app.state, "provider_key", None) and (settings.OPENROUTER_API_KEY is None or not settings.OPENROUTER_API_KEY.get_secret_value().strip()):
            log.error("TAGGER=openrouter 인데 PROFILING_OPENROUTER_API_KEY 가 없음 — 태그 추출기 끈 채로 진행")
            tagger_reason = "no_api_key"
        else:
            from profiling.llm import OpenRouterTagExtractor
            app.state.tagger = OpenRouterTagExtractor(settings.OPENROUTER_URL, settings.OPENROUTER_API_KEY.get_secret_value() if settings.OPENROUTER_API_KEY else "",
                                                      settings.tagger_model, timeout_s=settings.TAGGER_TIMEOUT_S)
            log.info("태그 추출기 openrouter model=%s url=%s", settings.tagger_model, settings.OPENROUTER_URL)   # 키는 남기지 않음
    if settings.TAG_ENGINE == "v3" and not stub_on:     # v3 엔진 — TAGGER 추출기는 v3 를 못 쓸 때의 폴백이 된다. 만들지 못하면 v2 추출기 그대로
        app.state.tagger, app.state.tag_spec = _v3_engine(settings, app.state.tagger, app.state.tag_spec)
    if settings.CATALOG_SOURCE == "search_http":
        app.state.embedding = app.state.catalog
        app.state.embedding_status = {"enabled": True, "version": app.state.embedding.version,
            "matched": app.state.embedding.matched, "total": app.state.embedding.total}
    elif settings.EMBEDDING_DIR:
        try:
            from profiling.embedding import DbEmbedding, ProductSearchEmbedding
            index = DbEmbedding if settings.EMBEDDING_SOURCE == "db" else ProductSearchEmbedding
            app.state.embedding = emb = index(Path(settings.EMBEDDING_DIR), app.state.engine)
            emb.encode(["상품 검색"])                       # 질의 인코더(Node)를 기동 때 띄워 둔다 — 첫 요청이 모델 적재를 기다리지 않게,
                                                            # 띄우지 못하면 여기서 실패해 임베딩을 끈 채로 뜬다(요청 중에 알게 되지 않게)
            app.state.embedding_status = {"enabled": True, "source": emb.source, "version": emb.version,
                                          "matched": emb.matched, "total": emb.total}
            log.info("임베딩 %s 벡터=%s 상품 매칭 %s/%s", emb.version, emb.source, emb.matched, emb.total)
        except Exception as e:
            log.exception("임베딩을 켜지 못함 — 끈 채로 진행(태그가 있어도 임베딩 단계 없이 D)")
            app.state.embedding_status = {"enabled": False, "reason": type(e).__name__}
    status.LLM.reset()
    status.LLM.configure(requested="openrouter-stub" if stub_on else
                         f"v3+{settings.TAGGER}" if getattr(app.state.tagger, "version", None) == "v3" else settings.TAGGER,
                         enabled=app.state.tagger is not None, model=getattr(app.state.tagger, "model", settings.tagger_model),
                         reason=tagger_reason)
    app.state.recipient_store = DbRecipientProfileStore(app.state.engine)

    app.state.io_limiter = anyio.CapacityLimiter(settings.IO_THREADS)   # /health · 조회 API — 기본 스레드풀(토큰 40)과 분리, 분석이 쌓여도 굶지 않는다
    log.info("profiling 시작 mq=%s pool_size=%s dislike_mode=%s pool_strategy=%s incremental_tags=%s", settings.MQ_ENABLED,
             settings.POOL_SIZE, settings.DISLIKE_MODE, settings.POOL_STRATEGY, settings.INCREMENTAL_TAGS)

    # RabbitMQ 요청 소비자 — 켤 때만. 설정이 빠졌거나 0 · 음수 · 모순이면 여기서 서버가 뜨지 않는다(설계 §7 "활성화를 거부")
    app.state.mq = None
    if settings.MQ_ENABLED:
        from profiling.mq import config as mqc
        from profiling.mq import runtime as mq_runtime
        try:
            app.state.mq = mq_runtime.build(app.state, settings, ready=lambda: all(_ready_checks(app.state).values()))
        except mqc.MqConfigError as e:
            app.state.engine.dispose()
            raise RuntimeError("PROFILING_MQ_ENABLED 인데 MQ 설정이 맞지 않음 — " + " · ".join(e.problems)) from e
        if not getattr(app.state, "mq_defer_start", False):     # 통합 런타임은 부품을 감싼 뒤 직접 start() 한다
            app.state.mq.start()

    yield

    # 종료 — MQ 소비자 먼저(새 소비 중단 → 진행 중 대기 → 연결 닫기, 미ACK 는 브로커가 재배달) — DB 풀을 닫기 전에.
    if app.state.mq is not None:
        app.state.mq.stop()
    log.info("profiling 종료")
    for part in (getattr(app.state, "tagger", None), getattr(app.state, "embedding", None)):
        close = getattr(part, "close", None)
        if callable(close):
            close()
    app.state.engine.dispose()


def _v3_engine(settings: Settings, fallback, spec):
    """v3 엔진 조립 — Modal v3 컨테이너 클라이언트 + 폴백 추출기. 주소 · 키(프록시 인증 둘 · 요청 키)가 비었거나 규격이 맞지 않으면
    v3 를 켜지 않고 (폴백 추출기, 규격)을 그대로 돌려준다(서버는 뜬다 — v2 로 돈다)."""
    from profiling import tagging
    spec = spec or tagging.load_spec()
    if not settings.V3_URL:
        log.error("TAG_ENGINE=v3(기본) 인데 V3_URL 이 비어 있음 — v2 추출기로만 진행(v3 를 안 쓸 거면 TAG_ENGINE=v2)")
        return fallback, spec
    if not (settings.V3_PROXY_KEY and settings.V3_PROXY_SECRET and settings.V3_API_KEY):   # Modal 은 프록시 인증 · 요청 키를 켠 채로 올림 — 없으면 모든 요청이 401
        log.error("TAG_ENGINE=v3(기본) 인데 V3_PROXY_KEY · V3_PROXY_SECRET · V3_API_KEY 중 빈 것이 있음 — v2 추출기로만 진행")
        return fallback, spec
    from profiling.v3.client import V3Client
    from profiling.v3.engine import V3Engine
    client = V3Client(settings.V3_URL, settings.V3_API_KEY.get_secret_value(),
                      proxy_key=settings.V3_PROXY_KEY.get_secret_value(), proxy_secret=settings.V3_PROXY_SECRET.get_secret_value())
    try:
        engine = V3Engine(client, spec, timeout_s=settings.V3_TIMEOUT_S,
                          fallback=fallback if settings.V3_FALLBACK else None)
    except ValueError as e:     # 규격이 비선호 규격이 아님 — 설정 오류
        client.close()
        log.error("v3 엔진을 만들지 못함: %s — v2 추출기로만 진행", e)
        return fallback, spec
    log.info("태그 엔진 v3 version=%s url=%s timeout_s=%s fallback=%s", engine.version, settings.V3_URL, settings.V3_TIMEOUT_S,
             engine.fallback.model if engine.fallback else "none")   # 키는 남기지 않음
    return engine, spec


def db_connections_needed(settings: Settings) -> int:
    """동시에 쥘 수 있는 커넥션 수 — IO_THREADS(/health · 조회 API) + MQ 슬롯마다 잠금 2 + 대조 복구 1(켰을 때)."""
    from profiling.mq.runtime import db_connections as mq_db_connections
    return settings.IO_THREADS + mq_db_connections(settings)


def db_pool_size(settings: Settings) -> int:
    """상시 커넥션 수 — 설정이 없으면 db_connections_needed(MQ 슬롯마다 잠금 커넥션을 분석 내내 쥐고, /health 가 그만큼 더 쓴다)."""
    return settings.DB_POOL_SIZE or db_connections_needed(settings)


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
    pool_size, need = db_pool_size(settings), db_connections_needed(settings)
    if pool_size + settings.DB_MAX_OVERFLOW < need:
        raise RuntimeError(f"DB 커넥션 풀이 작다: pool_size {pool_size} + max_overflow {settings.DB_MAX_OVERFLOW} < "
                           f"IO_THREADS {settings.IO_THREADS} + MQ 잠금 {need - settings.IO_THREADS}"
                           " — 슬롯이 커넥션을 기다리다 실패한다. "
                           "PROFILING_DB_POOL_SIZE 를 비우거나(자동) 늘린다")
    engine = sa.create_engine(
        _require_db_url(settings), pool_pre_ping=True, future=True,
        hide_parameters=True,      # 오류 메시지 · 로그에 바인드 값(정리본 글)을 싣지 않는다(10-17)
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
    except sa.exc.ProgrammingError as e:    # alembic_version 없음(마이그레이션 안 됨) · 읽을 권한 없음(앱 계정 권한이 아직 없음)
        engine.dispose()
        raise RuntimeError(f"DB는 있으나 마이그레이션 장부를 못 읽음({type(e.orig).__name__}) → uv run alembic upgrade head") from e
    if version != EXPECTED_MIGRATION and migration_ok(version):
        log.info("DB 마이그레이션 판 %s 가 코드(%s)보다 새 판 — 롤링 배포 중이면 정상(옛 코드도 도는 마이그레이션)", version, EXPECTED_MIGRATION)
    if not migration_ok(version):
        msg = f"DB 마이그레이션 판 {version} < 코드가 기대하는 {EXPECTED_MIGRATION} — 마이그레이션(alembic upgrade head)을 먼저 돌린다"
        if settings.STRICT_STARTUP:
            engine.dispose()
            raise RuntimeError(msg)
        log.warning("%s (로컬 — STRICT_STARTUP 이 아니라 계속 뜬다. /readyz 는 503)", msg)
    if superuser:
        if settings.DB_REFUSE_SUPERUSER:
            engine.dispose()
            raise RuntimeError(f"앱이 슈퍼유저 계정({user})으로 접속했다 — 앱 전용 계정으로 PROFILING_DATABASE_URL 을 바꾼다")
        log.warning("앱이 슈퍼유저 계정(%s)으로 접속 — 로컬 전용. 운영 · 개발 서버는 앱 전용 계정 + PROFILING_DB_REFUSE_SUPERUSER=true", user)
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
app.include_router(profile_read_router)


def _error_response(status: int, code: str, message: str, trace_id: str | None = None, *, retry_after: int | None = None) -> JSONResponse:
    """문서 1 §3 오류 봉투 {message, error: {code, traceId}}. 모든 오류 응답은 이 함수만 거친다.

    503 에는 `Retry-After` 를 붙인다(운영자 · 다른 호출자를 위한 표준 힌트). retry_after 가 오면 그 값, 없으면 RETRY_AFTER_S.
    """
    body = ErrorResponse(message=message, error=ErrorBody(code=code, traceId=trace_id))
    headers = None
    if status == 503:
        headers = {"Retry-After": str(retry_after if retry_after is not None else get_settings().RETRY_AFTER_S)}
    return JSONResponse(status_code=status, content=body.model_dump(), headers=headers)


@app.exception_handler(RequestValidationError)
async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Pydantic 검증 실패 — FastAPI 기본은 422이지만 문서 1은 400 INVALID_REQUEST.
    첫 번째 오류의 위치·사유를 message에 넣어 호출자가 무엇을 고칠지 알 수 있게 한다."""
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
    """잡히지 않은 예외 → 500 INTERNAL_SERVER_ERROR."""
    log.error("500 INTERNAL_SERVER_ERROR: %s", type(exc).__name__)
    return _error_response(500, "INTERNAL_SERVER_ERROR", "서버 오류가 발생했습니다.")


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    """**Liveness**(ECS) — 프로세스가 살아 있으면 늘 200. DB · 카탈로그 · 외부 LLM 을 보지 않는다(그것들이 죽어도 재시작으로 안 고쳐진다).
    검색 · 챗봇이 이미 쓰는 이름을 따랐다(10-15 프로파일링 담당 선택). BE 핑은 /health — 그대로 둔다."""
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz(request: Request) -> JSONResponse:
    """**Readiness**(ECS) — 요청을 받아 처리할 준비가 됐나(MQ 소비자도 이 검사가 거짓이면 소비를 멈춘다). 활성 카탈로그 · DB 연결 · 마이그레이션 판(DB ≥ EXPECTED_MIGRATION)이 다 맞으면 200,
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
        db, migration = True, migration_ok(version)
    except sa.exc.SQLAlchemyError:
        db = migration = False
    return {"catalog": catalog, "db": db, "migration": migration}


@app.get("/health", tags=["ops"])
async def health(request: Request) -> dict:
    """살아 있는지 + 활성 카탈로그가 있는지. 카탈로그가 없어도 200 — 프로세스는 살아 있으므로.

    `catalog.active` 와 `store.connected`(bool) 두 키는 옛 BE 핑 계약(v0.7 ①)에서 온 이름이다 — HTTP 7.6 이 없어진 뒤에도
    운영 · 통합 점검이 읽으므로 이름 · 뜻 · 타입을 그대로 둔다(tests/unit/test_health_contract.py).

    async 라 기본 스레드풀 토큰을 쓰지 않는다. DB 문장 셋은 io_limiter(별도 스레드)에서 — 실행 대기가 쌓여도 /health 는 답한다(09-28).
    """
    state = request.app.state
    return await anyio.to_thread.run_sync(_health_sync, state, limiter=state.io_limiter)


def _health_sync(state) -> dict:
    snapshot = getattr(state.catalog, "health_snapshot", None)
    if callable(snapshot):
        # Managed shared mode can rebind after startup. Read all generation
        # fields from one retained snapshot, not the startup embedding_status.
        projection = snapshot()
    else:
        try:
            version_id, products = state.catalog.active()
            catalog = {"active": True, "version": str(version_id), "products": len(products),
                       "productTags": getattr(state.catalog, "tags_state", "none")}
        except NoActiveCatalog as e:
            catalog = {"active": False, "reason": str(e)}
        projection = {"catalog": catalog, "embedding": getattr(state, "embedding_status", {"enabled": False})}
    mq = getattr(state, "mq", None)
    return {"status": "ok", **projection, "store": _store_health(state),
            "llm": status.LLM.snapshot(), "mq": mq.health() if mq is not None else {"enabled": False}}


def _store_health(state) -> dict:
    """저장소 상태 — 지금 연결되는지와 마이그레이션 버전. 확인 자체가 실패해도 /health는 200(프로세스는 살아 있으므로)."""
    engine = state.engine
    try:
        with engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")     # 읽기 한 문장 — BEGIN/ROLLBACK 없이
            version = conn.execute(sa.text("select version_num from alembic_version")).scalar()
        # 결과 전달(발행) 상태는 mq 칸(deliveries.open · dlq_pending)이 보여 준다
        return {"backend": "db", "connected": True, "migration": version, "migration_expected": EXPECTED_MIGRATION}
    except sa.exc.SQLAlchemyError as e:      # 연결 끊김·테이블 없음 등 DB 쪽 오류만 — 그 외는 500으로 드러나야 한다
        return {"backend": "db", "connected": False, "reason": type(e).__name__}
