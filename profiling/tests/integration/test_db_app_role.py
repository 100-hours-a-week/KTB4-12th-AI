"""앱 전용 계정 ai_app(0007) · 접속 설정(10-13 DB 검토) — 실제 DB 로.

  권한     소유자 계정으로 권한 함수(has_*_privilege)를 본다 + SET ROLE ai_app 으로 앱 경로(잠금 · 저장 · 읽기)를 실제로 돌린다
  접속     main._connect_db 가 세션에 시간 상한을 걸고 풀 크기를 슬롯에 맞추는가 · 슈퍼유저 거부 · 저장 잠금 대기가 lock_timeout 에서 끊기는가
DB 가 없으면 skip. 소유자가 ai_app 으로 SET ROLE 할 수 없으면(슈퍼유저도 멤버도 아님) 그 시험만 skip. 수신자 번호 9,500,001 ~ (DB_ERD §8)."""

import time

import pytest
import sqlalchemy as sa

from profiling import main, pipeline
from profiling import stores as stores_mod
from profiling.catalog import FileCatalogReader
from profiling.settings import Settings, get_settings
from profiling.stores import DbProfileRunStore, DbRecipientProfileStore
from profiling.types import (
    DislikedCategory,
    ProfileRequest,
    RecipientProfile,
    Review,
    RunStatus,
)

RID = 9_500_001
FIXTURE = "tests/fixtures/catalog_sample.json"
PROFILE_TABLES = ("ai_profile.recipient_profiles", "ai_profile.recipient_reviews", "ai_profile.profile_runs")
CATALOG_TABLES = ("ai_catalog.products", "ai_catalog.categories", "ai_catalog.catalog_versions")


@pytest.fixture(scope="module")
def engine():
    eng = sa.create_engine(get_settings().DATABASE_URL, future=True)
    try:
        with eng.connect() as c:
            c.execute(sa.text("select 1"))
    except sa.exc.OperationalError as e:
        pytest.skip(f"PostgreSQL에 연결할 수 없음: {type(e).__name__}")
    yield eng
    with eng.begin() as c:
        c.execute(sa.text("delete from ai_profile.profile_runs where recipient_user_id between :a and :b"), {"a": RID, "b": RID + 9})
        c.execute(sa.text("delete from ai_profile.recipient_profiles where recipient_user_id between :a and :b"), {"a": RID, "b": RID + 9})
    eng.dispose()


def _q(engine, sql: str, **kw):
    with engine.connect() as c:
        return c.execute(sa.text(sql), kw).scalar()


@pytest.fixture(scope="module")
def app_engine(engine):
    """SET ROLE ai_app 한 커넥션만 내주는 엔진 — 앱이 ai_app 으로 접속한 것과 같은 권한."""
    if not _q(engine, "select exists(select 1 from pg_roles where rolname = 'ai_app')"):
        pytest.skip("ai_app 역할 없음 — alembic upgrade head(0007) 전")
    if not _q(engine, "select pg_has_role(current_user, 'ai_app', 'MEMBER') or (select rolsuper from pg_roles where rolname = current_user)"):
        pytest.skip("소유자가 ai_app 으로 SET ROLE 할 수 없음")
    eng = sa.create_engine(get_settings().DATABASE_URL, future=True)

    @sa.event.listens_for(eng, "connect")
    def _as_app(dbapi_conn, _record):
        with dbapi_conn.cursor() as cur:
            cur.execute("SET ROLE ai_app")
        dbapi_conn.commit()                       # SET 은 트랜잭션을 따른다 — 커밋해야 세션 내내 남는다

    yield eng
    eng.dispose()


# ---------------------------------------------------------------- 권한 (0007)


def test_app_role_is_not_superuser_and_cannot_change_schema(engine) -> None:
    assert _q(engine, "select rolsuper or rolcreaterole or rolcreatedb from pg_roles where rolname = 'ai_app'") is False
    for schema in ("ai_profile", "ai_catalog", "public"):
        assert _q(engine, "select has_schema_privilege('ai_app', :s, 'CREATE')", s=schema) is False, schema
    assert _q(engine, "select has_schema_privilege('ai_app', 'ai_profile', 'USAGE') and has_schema_privilege('ai_app', 'ai_catalog', 'USAGE')")


@pytest.mark.parametrize("table", PROFILE_TABLES)
def test_app_role_reads_and_writes_profile_tables_but_cannot_truncate(engine, table) -> None:
    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert _q(engine, "select has_table_privilege('ai_app', :t, :p)", t=table, p=priv), (table, priv)
    assert _q(engine, "select has_table_privilege('ai_app', :t, 'TRUNCATE')", t=table) is False


@pytest.mark.parametrize("table", (*CATALOG_TABLES, "public.alembic_version"))
def test_app_role_only_reads_catalog_and_migration_ledger(engine, table) -> None:
    assert _q(engine, "select has_table_privilege('ai_app', :t, 'SELECT')", t=table)
    for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert _q(engine, "select has_table_privilege('ai_app', :t, :p)", t=table, p=priv) is False, (table, priv)


def test_app_path_runs_as_app_role(app_engine, engine) -> None:
    """ai_app 권한만으로 운영 경로가 끝까지 도는가 — 실행 잠금 · 기록 저장 · 수신자 저장 잠금 + CTE(리뷰 DELETE 포함) · 읽기."""
    runs, profiles = DbProfileRunStore(app_engine), DbRecipientProfileStore(app_engine)
    assert _q(app_engine, "select current_user") == "ai_app"
    rq = ProfileRequest(recipient_user_id=RID, source_version=1, gift_preference="향수 좋아해요",
                        disliked_categories=[DislikedCategory(category_id=802, category_name="출산·육아용품")],
                        reviews=[Review(product_id=13006, rating=5, review_text="향이 좋아요"), Review(product_id=13007, rating=2)])
    with runs.run_lock(RID, 1) as got:
        assert got
        out = pipeline.profile(rq, catalog=FileCatalogReader(FIXTURE), store=runs, recipient_store=profiles)
    assert out.status is RunStatus.RESULT_READY and runs.get_run(RID, 1).status is RunStatus.RESULT_READY
    profiles.upsert(RecipientProfile(recipient_user_id=RID, source_version=2))            # 리뷰를 다 지운 새 번호 — CTE 의 DELETE
    assert profiles.get(RID).reviews == []


def test_app_role_cannot_write_catalog_or_change_tables(app_engine) -> None:
    for sql in ("update ai_catalog.products set view_count = view_count where false",
                "create table ai_profile.should_not_exist (i int)",
                "truncate ai_profile.recipient_reviews"):
        with app_engine.connect() as c, pytest.raises(sa.exc.ProgrammingError, match="permission denied|InsufficientPrivilege"):
            c.execute(sa.text(sql))


# ---------------------------------------------------------------- 접속 설정 (main._connect_db)


def _settings(**kw) -> Settings:
    return Settings(DATABASE_URL=get_settings().DATABASE_URL, **kw)


def test_connect_db_sets_session_timeouts_and_pool_size(engine) -> None:
    eng = main._connect_db(_settings(PROFILING_SLOTS=3, IO_THREADS=2, DB_STATEMENT_TIMEOUT_MS=4321, DB_LOCK_TIMEOUT_MS=321))
    try:
        with eng.connect() as c:
            assert c.execute(sa.text("show statement_timeout")).scalar() == "4321ms"
            assert c.execute(sa.text("show lock_timeout")).scalar() == "321ms"
            assert c.execute(sa.text("show idle_in_transaction_session_timeout")).scalar() == "1min"
        assert eng.pool.size() == 3 + 2
    finally:
        eng.dispose()


def test_connect_db_refuses_superuser_when_asked(engine) -> None:
    if not _q(engine, "select rolsuper from pg_roles where rolname = current_user"):
        pytest.skip("시험 계정이 슈퍼유저가 아님")
    with pytest.raises(RuntimeError, match="슈퍼유저"):
        main._connect_db(_settings(DB_REFUSE_SUPERUSER=True))


def test_save_lock_wait_is_cut_by_lock_timeout(engine) -> None:
    """다른 저장이 수신자 잠금을 놓지 않아도 저장이 영영 기다리지 않는다 — lock_timeout 에서 예외(파이프라인은 FAILED 로 기록)."""
    eng = main._connect_db(_settings(DB_LOCK_TIMEOUT_MS=300))
    key = stores_mod._profile_lock_key(RID + 1)
    holder = engine.connect()
    holder.execute(stores_mod._PROFILE_LOCK, {"k": key})
    try:
        t0 = time.monotonic()
        with pytest.raises(sa.exc.OperationalError, match="lock timeout|LockNotAvailable"):
            DbRecipientProfileStore(eng).upsert(RecipientProfile(recipient_user_id=RID + 1, source_version=1))
        assert time.monotonic() - t0 < 3
    finally:
        holder.execute(stores_mod._PROFILE_UNLOCK, {"k": key})
        holder.close()
        eng.dispose()
