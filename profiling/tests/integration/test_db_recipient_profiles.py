"""로컬 PostgreSQL(docker compose) 통합 시험 — ai_profile.recipient_profiles·recipient_reviews·profile_runs 가 마이그레이션대로 있고 upsert·상태 규칙이 도는지.
DB가 안 떠 있으면 skip (단위 테스트와 섞이지 않게). 실행: docker compose up -d && uv run alembic upgrade head && uv run pytest tests/integration"""

import json

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


def test_schema_applied(conn, alembic_head) -> None:
    cols = conn.execute(sa.text("""
        select column_name, data_type, column_default from information_schema.columns
        where table_schema='ai_profile' and table_name='recipient_profiles' order by ordinal_position""")).all()
    names = [c[0] for c in cols]
    assert names == ["recipient_user_id", "source_version", "preferred_tags", "disliked_categories", "created_at", "updated_at",
                     "gift_preference_hash", "category_scores",                 # ADD COLUMN 은 뒤에 붙는다
                     "gift_preference_clean", "guard_version", "tag_status", "prompt_version", "validator_version",
                     "recommended_product_ids", "catalog_version_id"]          # 챗봇 조회용(정리본 · 상태 · 30개). disliked_tags 는 지움(DR-040)
    idx = {r[0] for r in conn.execute(sa.text("select indexname from pg_indexes where schemaname='ai_profile' and tablename='recipient_profiles'"))}
    assert "ix_recipient_profiles_preferred_tags" in idx and "ix_recipient_profiles_disliked_tags" not in idx
    assert {c[0]: c[2] for c in cols}["category_scores"].startswith("'[]'::jsonb")
    assert cols[0][2] is None                                       # PK는 Backend 값 — 시퀀스 없음
    assert conn.execute(sa.text("select 1 from pg_extension where extname='vector'")).scalar() == 1
    assert conn.execute(sa.text("select version_num from alembic_version")).scalar() == alembic_head


UPSERT = sa.text("""
    insert into ai_profile.recipient_profiles (recipient_user_id, source_version, preferred_tags, disliked_categories)
    values (:rid, :sv, cast(:pref as jsonb), cast(:cats as jsonb))
    on conflict (recipient_user_id) do update
      set source_version = excluded.source_version, preferred_tags = excluded.preferred_tags,
          disliked_categories = excluded.disliked_categories, updated_at = now()
      where ai_profile.recipient_profiles.source_version <= excluded.source_version""")


def _tag(tag: str) -> str:
    return json.dumps([{"axis": "카테고리", "tag": tag, "source": "preference", "ref": "pref"}], ensure_ascii=False)


def test_upsert_keeps_newer_version(conn) -> None:
    conn.execute(UPSERT, {"rid": 1, "sv": 3, "pref": _tag("휴대용"), "cats": '[{"category_id":9040,"category_name":"도서·음반"}]'})
    conn.execute(UPSERT, {"rid": 1, "sv": 2, "pref": _tag("옛것"), "cats": "[]"})                 # 순서 역전 → 무시
    row = conn.execute(sa.text("select source_version, preferred_tags -> 0 ->> 'tag' from ai_profile.recipient_profiles where recipient_user_id=1")).one()
    assert row == (3, "휴대용")
    conn.execute(UPSERT, {"rid": 1, "sv": 4, "pref": _tag("새것"), "cats": "[]"})                 # 더 새 버전 → 덮어씀
    assert conn.execute(sa.text("select preferred_tags -> 0 ->> 'tag' from ai_profile.recipient_profiles where recipient_user_id=1")).scalar() == "새것"


# ---- recipient_reviews · 리뷰 신호 열 — 원문은 저장하지 않는다(10-11 결정 B) ---------------------------------------
# 수신자 번호는 9_000_001 부터 — 로컬 DB 에 콘솔 시험 데이터(작은 번호)가 남아 있어도 겹치지 않게

R1 = 9_000_001
INSERT_PROFILE = sa.text("insert into ai_profile.recipient_profiles (recipient_user_id, source_version) values (:rid, 1)")
INSERT_REVIEW = sa.text("""
    insert into ai_profile.recipient_reviews (recipient_user_id, product_id, rating, review_hash, position)
    values (:rid, :pid, :rating, :h, :pos)""")


def test_recipient_reviews_schema(conn) -> None:
    cols = conn.execute(sa.text("""
        select column_name, col_description(('ai_profile.' || table_name)::regclass, ordinal_position)
        from information_schema.columns where table_schema='ai_profile' and table_name='recipient_reviews' order by ordinal_position""")).all()
    assert [c[0] for c in cols] == ["recipient_user_id", "product_id", "rating", "review_hash", "position", "created_at", "updated_at",
                                    "review_text_clean"]                       # 리뷰 글 정리본
    assert all(c[1] for c in cols if c[0] not in ("created_at", "updated_at"))   # 업무 열마다 주석 (팀 규칙)
    pk = conn.execute(sa.text("""
        select string_agg(a.attname, ',' order by array_position(i.indkey, a.attnum))
        from pg_index i join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)
        where i.indrelid = 'ai_profile.recipient_reviews'::regclass and i.indisprimary""")).scalar()
    assert pk == "recipient_user_id,product_id"                                  # 상품당 한 행
    fk = conn.execute(sa.text("""
        select confdeltype from pg_constraint where conname = 'fk_recipient_reviews_profile'""")).scalar()
    assert fk == "c"                                                              # ON DELETE CASCADE


def test_no_raw_text_columns_stored(conn) -> None:
    """위키: 7.6 입력은 그 실행에서만 쓰고 저장하지 않는다 — 원문 열이 두 표 어디에도 없어야 한다(10-11 결정 B)."""
    names = {r[0] for r in conn.execute(sa.text("""
        select column_name from information_schema.columns
        where table_schema='ai_profile' and table_name in ('recipient_profiles', 'recipient_reviews')""")).all()}
    assert not names & {"review_text", "gift_preference", "preference", "text"}


def test_recipient_reviews_cascade_and_checks(conn) -> None:
    conn.execute(INSERT_PROFILE, {"rid": R1})
    conn.execute(INSERT_REVIEW, {"rid": R1, "pid": 11, "rating": 5, "h": "a" * 64, "pos": 0})
    with pytest.raises(sa.exc.IntegrityError, match="ck_recipient_reviews_rating"), conn.begin_nested():
        conn.execute(INSERT_REVIEW, {"rid": R1, "pid": 12, "rating": 0, "h": "b" * 64, "pos": 1})
    with pytest.raises(sa.exc.IntegrityError, match="ck_recipient_reviews_position"), conn.begin_nested():
        conn.execute(INSERT_REVIEW, {"rid": R1, "pid": 13, "rating": 3, "h": "c" * 64, "pos": 10})
    with pytest.raises(sa.exc.IntegrityError, match="pk_recipient_reviews"), conn.begin_nested():
        conn.execute(INSERT_REVIEW, {"rid": R1, "pid": 11, "rating": 1, "h": "d" * 64, "pos": 2})   # 같은 상품 두 번
    with pytest.raises(sa.exc.IntegrityError, match="fk_recipient_reviews_profile"), conn.begin_nested():
        conn.execute(INSERT_REVIEW, {"rid": R1 + 1, "pid": 11, "rating": 5, "h": "e" * 64, "pos": 0})  # 부모 프로필 없음
    conn.execute(sa.text("delete from ai_profile.recipient_profiles where recipient_user_id = :rid"), {"rid": R1})
    left = conn.execute(sa.text("select count(*) from ai_profile.recipient_reviews where recipient_user_id = :rid"), {"rid": R1}).scalar()
    assert left == 0                                                              # 부모를 지우면 자식도 사라진다


# ---- profile_runs -------------------------------------------------------------------------------------------------

def test_profile_runs_schema(conn) -> None:
    names = [r[0] for r in conn.execute(sa.text("""
        select column_name from information_schema.columns
        where table_schema='ai_profile' and table_name='profile_runs' order by ordinal_position""")).all()]
    assert names == ["id", "recipient_user_id", "source_version", "input_hash", "status", "attempt", "catalog_version_id",
                     "callback_payload", "callback_hash", "callback_attempts", "error", "created_at", "updated_at",
                     "tag_status", "prompt_version", "validator_version", "llm_cost_usd"]
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
