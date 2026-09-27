"""DB adapter 통합 시험 — DbProfileRunStore(profile_runs) · DbRecipientProfileStore(recipient_profiles) · 앱 전체.
DB가 안 떠 있으면 skip. 실행: docker compose up -d && uv run alembic upgrade head && uv run pytest tests/integration -q

adapter가 트랜잭션을 스스로 열므로(engine.begin) 테스트를 트랜잭션으로 감쌀 수 없다 → 시험용 수신자 ID(99xxxx)를 쓰고 끝에 지운다."""

from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from profiling import intake, pipeline
from profiling.catalog import (
    FILE_CATALOG_VERSION_ID,
    FileCatalogReader,
)
from profiling.ports import ProfileRunStore, RecipientProfileStore
from profiling.settings import get_settings
from profiling.stores import DbProfileRunStore, DbRecipientProfileStore
from profiling.types import (
    CallbackResult,
    DislikedCategory,
    ErrorCode,
    ProfileOutcome,
    ProfileRequest,
    RecipientProfile,
    RunStatus,
    SearchResult,
)

RID = 990_001
FIXTURE = "tests/fixtures/catalog_sample.json"


@pytest.fixture(scope="module")
def engine():
    eng = sa.create_engine(get_settings().DATABASE_URL, future=True)
    try:
        with eng.connect() as c:
            c.execute(sa.text("select 1"))
    except sa.exc.OperationalError as e:
        pytest.skip(f"PostgreSQL에 연결할 수 없음: {type(e).__name__}")
    yield eng
    eng.dispose()


@pytest.fixture
def stores(engine):
    runs, profiles = DbProfileRunStore(engine), DbRecipientProfileStore(engine)
    yield runs, profiles
    for rid in (RID, RID + 1, RID + 2):        # 정리 — 시험용 수신자만
        runs.delete_recipient(rid)
        profiles.delete(rid)


def _rq(rid=RID, sv=3, disliked=((802, "출산·육아용품"),)) -> ProfileRequest:
    return ProfileRequest(recipient_user_id=rid, source_version=sv, gift_preference=None,
                          disliked_categories=[DislikedCategory(category_id=i, category_name=n) for i, n in disliked], reviews=[])


def _count(engine, table, rid) -> int:
    with engine.connect() as c:
        return c.execute(sa.text(f"select count(*) from ai_profile.{table} where recipient_user_id=:rid"), {"rid": rid}).scalar()


# ---------------------------------------------------------------- DbProfileRunStore


def test_run_store_shape(stores) -> None:
    runs, profiles = stores
    assert isinstance(runs, ProfileRunStore) and isinstance(profiles, RecipientProfileStore)


def test_run_store_lifecycle_running_result_delivered(stores, engine) -> None:
    runs, _ = stores
    rq = _rq()
    h = pipeline.input_hash(rq)
    runs.save(ProfileOutcome(recipient_user_id=RID, source_version=3, status=RunStatus.RUNNING, input_hash=h))
    got = runs.get(RID)
    assert got.status is RunStatus.RUNNING and got.search is None and got.input_hash == h

    ready = ProfileOutcome(recipient_user_id=RID, source_version=3, status=RunStatus.RESULT_READY, input_hash=h,
                           search=SearchResult(product_ids=[5, 3, 9], query_text="", catalog_version_id=FILE_CATALOG_VERSION_ID))
    runs.save(ready)
    got = runs.get(RID)
    assert got.status is RunStatus.RESULT_READY and got.search.product_ids == [5, 3, 9] and got.search.catalog_version_id == FILE_CATALOG_VERSION_ID

    runs.save(ready.model_copy(update={"status": RunStatus.DELIVERED, "callback_attempts": 1}))
    got = runs.get(RID)
    assert got.status is RunStatus.DELIVERED and got.callback_attempts == 1 and got.search.product_ids == [5, 3, 9]   # payload 유지
    with engine.connect() as c:
        row = c.execute(sa.text("select attempt, callback_hash, callback_payload->'recommendedProductIds' from ai_profile.profile_runs "
                                "where recipient_user_id=:rid and source_version=3"), {"rid": RID}).one()
    assert row[0] == 1 and len(row[1]) == 64 and row[2] == [5, 3, 9]
    assert _count(engine, "profile_runs", RID) == 1                                                       # 같은 (수신자, 버전) = 한 행


def test_run_store_rerun_increments_attempt_and_failed_keeps_reason(stores, engine) -> None:
    runs, _ = stores
    base = ProfileOutcome(recipient_user_id=RID, source_version=1, status=RunStatus.RUNNING, input_hash="h")
    runs.save(base)
    runs.save(base.model_copy(update={"status": RunStatus.FAILED, "failure_code": ErrorCode.NO_ACTIVE_CATALOG, "failure_reason": "활성 카탈로그 없음"}))
    got = runs.get(RID)
    assert got.failure_reason == "활성 카탈로그 없음" and got.failure_code is ErrorCode.NO_ACTIVE_CATALOG   # error = {code, reason}
    runs.save(base)                                                                                       # 재실행 → attempt 2, error 비움
    with engine.connect() as c:
        assert c.execute(sa.text("select attempt, error from ai_profile.profile_runs where recipient_user_id=:rid"), {"rid": RID}).one() == (2, None)


def test_run_store_get_returns_latest_version(stores) -> None:
    runs, _ = stores
    for sv in (2, 5, 3):
        runs.save(ProfileOutcome(recipient_user_id=RID, source_version=sv, status=RunStatus.RUNNING, input_hash="h"))
    assert runs.get(RID).source_version == 5


def test_run_store_requires_input_hash(stores) -> None:
    runs, _ = stores
    with pytest.raises(ValueError, match="input_hash"):
        runs.save(ProfileOutcome(recipient_user_id=RID, source_version=1, status=RunStatus.RUNNING))


# ---------------------------------------------------------------- DbRecipientProfileStore


def test_profile_store_upsert_get_delete_and_version_guard(stores) -> None:
    _, profiles = stores
    p3 = RecipientProfile(recipient_user_id=RID, source_version=3, disliked_tags=["도서·음반"],
                          disliked_categories=[DislikedCategory(category_id=701, category_name="도서·음반")])
    profiles.upsert(p3)
    got = profiles.get(RID)
    assert got.source_version == 3 and got.disliked_tags == ["도서·음반"] and got.disliked_categories == p3.disliked_categories
    assert got.updated_at is not None and got.preferred_tags == []

    profiles.upsert(RecipientProfile(recipient_user_id=RID, source_version=2, disliked_tags=["옛것"]))     # 순서 역전 → 무시
    assert profiles.get(RID).disliked_tags == ["도서·음반"]
    profiles.upsert(RecipientProfile(recipient_user_id=RID, source_version=4, disliked_tags=["새것"]))     # 더 새 버전 → 덮음
    assert profiles.get(RID).disliked_tags == ["새것"]

    assert profiles.delete(RID) is True and profiles.get(RID) is None and profiles.delete(RID) is False


# ---------------------------------------------------------------- pipeline → 두 테이블


def test_pipeline_writes_both_tables(stores, engine) -> None:
    runs, profiles = stores
    rq = _rq(rid=RID + 1)
    out = pipeline.profile(rq, catalog=FileCatalogReader(FIXTURE), store=runs, recipient_store=profiles)
    assert out.status is RunStatus.RESULT_READY and len(out.search.product_ids) == 30
    run = runs.get(RID + 1)
    assert run.status is RunStatus.RESULT_READY and run.search.product_ids == out.search.product_ids and run.input_hash == out.input_hash
    prof = profiles.get(RID + 1)
    assert prof.source_version == 3 and [c.category_id for c in prof.disliked_categories] == [802] and prof.disliked_tags == ["출산·육아용품"]


# ---------------------------------------------------------------- 앱 전체 (진짜 DB)


class RecordingBackend:
    def __init__(self, result=None):
        self.sent, self.result = [], result or CallbackResult(status=RunStatus.DELIVERED, http_status=200)

    def send_profile_callback(self, outcome):
        self.sent.append(outcome)
        return self.result


def test_app_end_to_end_with_db(engine, monkeypatch, alembic_head) -> None:
    monkeypatch.setenv("PROFILING_CATALOG_FILE", FIXTURE)
    get_settings.cache_clear()
    from profiling.main import app
    rid = RID + 2
    try:
        with TestClient(app) as client:
            assert isinstance(app.state.store, DbProfileRunStore) and isinstance(app.state.recipient_store, DbRecipientProfileStore)
            health = client.get("/health").json()
            assert health["store"]["backend"] == "db" and health["store"]["connected"] is True
            assert health["store"]["migration"] == alembic_head
            assert isinstance(health["store"]["undelivered"], int)   # 결과는 있는데 못 보낸 행 수

            fake = RecordingBackend()
            app.state.backend = fake
            body = {"recipientUserId": rid, "sourceVersion": 7, "dislikedCategories": [{"categoryId": 802, "categoryName": "출산·육아용품"}],
                    "giftPreference": None, "reviews": []}
            res = client.post(intake.EXTRACT_AND_POOL_PATH, json=body)
            assert res.status_code == 202 and len(fake.sent) == 1

            run = app.state.store.get(rid)                       # 콜백 200 → DELIVERED 가 DB에
            assert run.status is RunStatus.DELIVERED and run.callback_attempts == 1 and run.source_version == 7
            assert isinstance(run.search.catalog_version_id, UUID) and len(run.search.product_ids) == 30
            prof = app.state.recipient_store.get(rid)
            assert prof.source_version == 7 and prof.disliked_categories == [DislikedCategory(category_id=802, category_name="출산·육아용품")]
    finally:
        DbProfileRunStore(engine).delete_recipient(rid)
        DbRecipientProfileStore(engine).delete(rid)
        get_settings.cache_clear()


# ---------------------------------------------------------------- 같은 키 동시 처리 막기


def test_run_lock_is_exclusive_across_connections(engine) -> None:
    """advisory lock 은 **커넥션(=프로세스) 밖에서도** 통해야 한다.

    이게 없으면 같은 (수신자, 버전)이 동시에 들어올 때 둘 다 "기록 없음"을 보고 둘 다 분석한다.
    Backend 는 같은 번호로 최대 2회 재전송하므로 실제로 겹칠 수 있다.
    """
    rid, sv = 991_001, 7
    a, b = DbProfileRunStore(engine), DbProfileRunStore(engine)
    with a.run_lock(rid, sv) as first:
        assert first is True
        with b.run_lock(rid, sv) as second:
            assert second is False                    # 두 번째는 기다리지 않고 바로 포기한다
        with b.run_lock(rid, sv + 1) as other_key:
            assert other_key is True                  # 다른 키는 서로 막지 않는다
    with b.run_lock(rid, sv) as after:
        assert after is True                          # 풀리면 다시 잡힌다


def test_run_lock_releases_on_exception(engine) -> None:
    """작업이 예외로 끝나도 잠금이 남으면 그 수신자는 영영 막힌다."""
    rid, sv = 991_002, 1
    store = DbProfileRunStore(engine)
    with pytest.raises(RuntimeError), store.run_lock(rid, sv) as got:
        assert got is True
        raise RuntimeError("작업 실패")
    with store.run_lock(rid, sv) as again:
        assert again is True


# ---------------------------------------------------------------- 시작 시 정리 · 미전달 수


def test_recover_stale_runs_only_touches_old_running(engine, stores) -> None:
    """끊긴 실행만 FAILED 로. **지금 돌고 있는 것(신선한 RUNNING)은 건드리지 않는다** —
    다른 인스턴스가 처리 중인 행을 죽이면 그 요청이 통째로 사라진다."""
    runs, _ = stores
    rid_old, rid_new = RID + 40, RID + 41
    try:
        for rid in (rid_old, rid_new):
            runs.save(ProfileOutcome(recipient_user_id=rid, source_version=1, status=RunStatus.RUNNING, input_hash="h"))
        with engine.begin() as c:   # 하나만 과거로 밀어 "끊긴 실행"을 만든다
            c.execute(sa.text("update ai_profile.profile_runs set updated_at = now() - interval '1 hour' "
                              "where recipient_user_id = :r"), {"r": rid_old})

        assert runs.recover_stale_runs(300) == 1
        assert runs.get(rid_old).status is RunStatus.FAILED
        assert runs.get(rid_old).failure_reason and "시작 시 정리" in runs.get(rid_old).failure_reason
        assert runs.get(rid_new).status is RunStatus.RUNNING      # 신선한 것은 그대로
        assert runs.recover_stale_runs(300) == 0                  # 두 번째 호출은 할 일이 없다
    finally:
        for rid in (rid_old, rid_new):
            runs.delete_recipient(rid)


def test_undelivered_count_counts_result_ready(engine, stores) -> None:
    """결과는 있는데 못 보낸 행(RESULT_READY)만 센다 — /health 가 이 숫자를 보여준다."""
    runs, _ = stores
    rid = RID + 42
    search = SearchResult(product_ids=[1, 2], query_text="", catalog_version_id=FILE_CATALOG_VERSION_ID)
    try:
        before = runs.undelivered_count()
        runs.save(ProfileOutcome(recipient_user_id=rid, source_version=1, status=RunStatus.RESULT_READY,
                                 input_hash="h", search=search))
        assert runs.undelivered_count() == before + 1
        runs.save(ProfileOutcome(recipient_user_id=rid, source_version=1, status=RunStatus.DELIVERED,
                                 input_hash="h", search=search, callback_attempts=1))
        assert runs.undelivered_count() == before      # 전달되면 빠진다
    finally:
        runs.delete_recipient(rid)
