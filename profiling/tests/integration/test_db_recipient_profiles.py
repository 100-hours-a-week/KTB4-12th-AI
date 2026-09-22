"""로컬 PostgreSQL(docker compose) 통합 시험 — ai_profile.recipient_profiles·profile_runs 가 마이그레이션대로 있고 upsert·상태 규칙이 도는지.
DB가 안 떠 있으면 skip (단위 테스트와 섞이지 않게). 실행: docker compose up -d && uv run alembic upgrade head && uv run pytest tests/integration"""

import pytest
import sqlalchemy as sa

from profiling.settings import get_settings


@pytest.fixture(scope="module")
def engine():
    eng = sa.create_engine(get_settings().DATABASE_URL, future=True)
    try:
        with eng.connect() as c:
            c.execute(sa.text("select 1"))
    except sa.exc.OperationalError as e:  # DB 없음 → skip
        pytest.skip(f"PostgreSQL에 연결할 수 없음: {type(e).__name__}")
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine):
    with engine.begin() as c:                      # 테스트마다 트랜잭션 → 끝나면 롤백 (DB를 더럽히지 않음)
        yield c
        c.rollback()


def test_schema_applied(conn) -> None:
    cols = conn.execute(sa.text("""
        select column_name, data_type, column_default from information_schema.columns
        where table_schema='ai_profile' and table_name='recipient_profiles' order by ordinal_position""")).all()
    names = [c[0] for c in cols]
    assert names == ["recipient_user_id", "source_version", "preferred_tags", "disliked_tags", "disliked_categories", "created_at", "updated_at"]
    assert cols[0][2] is None                                       # PK는 Backend 값 — 시퀀스 없음
    assert conn.execute(sa.text("select 1 from pg_extension where extname='vector'")).scalar() == 1
    assert conn.execute(sa.text("select version_num from alembic_version")).scalar() == "0002"


UPSERT = sa.text("""
    insert into ai_profile.recipient_profiles (recipient_user_id, source_version, preferred_tags, disliked_tags, disliked_categories)
    values (:rid, :sv, cast(:pref as jsonb), cast(:dis as jsonb), cast(:cats as jsonb))
    on conflict (recipient_user_id) do update
      set source_version = excluded.source_version, preferred_tags = excluded.preferred_tags,
          disliked_tags = excluded.disliked_tags, disliked_categories = excluded.disliked_categories, updated_at = now()
      where ai_profile.recipient_profiles.source_version <= excluded.source_version""")


def test_upsert_keeps_newer_version(conn) -> None:
    conn.execute(UPSERT, {"rid": 1, "sv": 3, "pref": '["휴대용"]', "dis": '["도서·음반"]', "cats": '[{"category_id":701,"category_name":"도서·음반"}]'})
    conn.execute(UPSERT, {"rid": 1, "sv": 2, "pref": '["옛것"]', "dis": "[]", "cats": "[]"})       # 순서 역전 → 무시
    row = conn.execute(sa.text("select source_version, preferred_tags from ai_profile.recipient_profiles where recipient_user_id=1")).one()
    assert row == (3, ["휴대용"])
    conn.execute(UPSERT, {"rid": 1, "sv": 4, "pref": '["새것"]', "dis": "[]", "cats": "[]"})       # 더 새 버전 → 덮어씀
    assert conn.execute(sa.text("select preferred_tags from ai_profile.recipient_profiles where recipient_user_id=1")).scalar() == ["새것"]


# ---- profile_runs (0002) -------------------------------------------------------------------------------------------------

def test_profile_runs_schema(conn) -> None:
    names = [r[0] for r in conn.execute(sa.text("""
        select column_name from information_schema.columns
        where table_schema='ai_profile' and table_name='profile_runs' order by ordinal_position""")).all()]
    assert names == ["id", "recipient_user_id", "source_version", "input_hash", "status", "attempt", "catalog_version_id",
                     "callback_payload", "callback_hash", "callback_attempts", "error", "created_at", "updated_at"]
    idx = conn.execute(sa.text("select indexdef from pg_indexes where schemaname='ai_profile' and indexname='unique_profile_source'")).scalar()
    assert idx and "UNIQUE" in idx and "(recipient_user_id, source_version)" in idx


INSERT_RUN = sa.text("""
    insert into ai_profile.profile_runs (recipient_user_id, source_version, input_hash, status)
    values (:rid, :sv, :h, 'RUNNING') returning id""")


def test_profile_runs_lifecycle(conn) -> None:
    run_id = conn.execute(INSERT_RUN, {"rid": 7, "sv": 1, "h": "h1"}).scalar()            # 접수 → RUNNING, id는 DB가 생성
    assert run_id is not None
    # §16.4 — 결과 없이 RESULT_READY 불가 (CHECK)
    with pytest.raises(sa.exc.IntegrityError, match="ck_profile_runs_result_has_payload"), conn.begin_nested():
        conn.execute(sa.text("update ai_profile.profile_runs set status='RESULT_READY' where id=:id"), {"id": run_id})
    # payload·hash와 함께 RESULT_READY → 콜백 200 → DELIVERED
    conn.execute(sa.text("""update ai_profile.profile_runs
        set status='RESULT_READY', callback_payload=cast(:p as jsonb), callback_hash=:ph, updated_at=now() where id=:id"""),
        {"id": run_id, "p": '{"recommendedProductIds":[1,2,3],"sourceVersion":1}', "ph": "p1"})
    conn.execute(sa.text("update ai_profile.profile_runs set status='DELIVERED', callback_attempts=callback_attempts+1 where id=:id"), {"id": run_id})
    row = conn.execute(sa.text("select status, callback_attempts, callback_payload->'recommendedProductIds' from ai_profile.profile_runs where id=:id"),
                       {"id": run_id}).one()
    assert row == ("DELIVERED", 1, [1, 2, 3])


def test_profile_runs_rejects_duplicate_key_and_bad_status(conn) -> None:
    conn.execute(INSERT_RUN, {"rid": 8, "sv": 5, "h": "h1"})
    with pytest.raises(sa.exc.IntegrityError, match="unique_profile_source"), conn.begin_nested():   # 같은 수신자·같은 버전 두 번 접수 불가
        conn.execute(INSERT_RUN, {"rid": 8, "sv": 5, "h": "h2"})
    with pytest.raises(sa.exc.IntegrityError, match="ck_profile_runs_status"), conn.begin_nested():  # RunStatus 밖의 값 불가
        conn.execute(sa.text("insert into ai_profile.profile_runs (recipient_user_id, source_version, input_hash, status) values (9, 1, 'h', 'DONE')"))
