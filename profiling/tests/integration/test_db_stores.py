"""DB adapter 통합 시험 — DbProfileRunStore(profile_runs) · DbRecipientProfileStore(recipient_profiles) · 앱 전체.
DB가 안 떠 있으면 skip. 실행: docker compose up -d && uv run alembic upgrade head && uv run pytest tests/integration -q

adapter가 트랜잭션을 스스로 열므로(engine.begin) 테스트를 트랜잭션으로 감쌀 수 없다 → 시험용 수신자 ID(99xxxx)를 쓰고 끝에 지운다."""

import time
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
    CategoryScore,
    DislikedCategory,
    ErrorCode,
    ProfileOutcome,
    ProfileRequest,
    ProfileTag,
    RecipientProfile,
    Review,
    ReviewRecord,
    RunStatus,
    SearchResult,
    preference_hash,
    review_hash,
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
    for rid in range(RID, RID + 6):           # 정리 — 시험용 수신자만 (리뷰 자식 행은 CASCADE 로 함께)
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
    def dis(cid: int, name: str) -> list[DislikedCategory]:
        return [DislikedCategory(category_id=cid, category_name=name)]

    p3 = RecipientProfile(recipient_user_id=RID, source_version=3, disliked_categories=dis(701, "도서·음반"),
                          preferred_tags=[ProfileTag(axis="IP", tag="산리오 한교동", source="preference", ref="pref")])
    profiles.upsert(p3)
    got = profiles.get(RID)
    assert got.source_version == 3 and got.disliked_categories == p3.disliked_categories and got.preferred_tags == p3.preferred_tags
    assert got.updated_at is not None

    profiles.upsert(RecipientProfile(recipient_user_id=RID, source_version=2, disliked_categories=dis(1, "옛것")))   # 순서 역전 → 무시
    assert profiles.get(RID).disliked_categories == dis(701, "도서·음반")
    profiles.upsert(RecipientProfile(recipient_user_id=RID, source_version=4, disliked_categories=dis(2, "새것")))   # 더 새 버전 → 덮음
    assert profiles.get(RID).disliked_categories == dis(2, "새것") and profiles.get(RID).preferred_tags == []

    assert profiles.delete(RID) is True and profiles.get(RID) is None and profiles.delete(RID) is False


# ---------------------------------------------------------------- 프로필 + 리뷰 한 문장 저장 (0005 · v2 4단계 · CTE)


def _rec(pid: int, rating: int, pos: int, text: str | None = None) -> ReviewRecord:
    return ReviewRecord(product_id=pid, rating=rating, position=pos,
                        review_hash=review_hash(Review(product_id=pid, rating=rating, review_text=text)))


def _prof(sv: int, reviews: list[ReviewRecord], *, rid: int = RID + 3, scores=(), pref: str | None = None) -> RecipientProfile:
    return RecipientProfile(recipient_user_id=rid, source_version=sv, reviews=reviews, category_scores=list(scores),
                            gift_preference_hash=preference_hash(pref))


def _review_rows(engine, rid: int) -> list[tuple]:
    with engine.connect() as c:
        return [tuple(r) for r in c.execute(sa.text(
            "select product_id, rating, position, review_hash from ai_profile.recipient_reviews where recipient_user_id=:rid order by position"),
            {"rid": rid}).all()]


def test_profile_store_first_save_writes_both_tables_and_get_restores(stores, engine) -> None:
    """프로필이 없던 수신자 — 부모와 자식을 한 문장에서 만든다(FK 는 문장 끝에 검사). get() 이 리뷰 순서 · 점수 · 지문을 되살린다."""
    _, profiles = stores
    scores = [CategoryScore(category_id=11, n=1, score_sum=2)]
    p = _prof(1, [_rec(7, 5, 0, "좋아요"), _rec(8, 2, 1)], scores=scores, pref="향수 좋아해요")
    profiles.upsert(p)
    got = profiles.get(RID + 3)
    assert got.reviews == p.reviews and got.category_scores == scores
    assert got.gift_preference_hash == preference_hash("향수 좋아해요")
    assert [r[:3] for r in _review_rows(engine, RID + 3)] == [(7, 5, 0), (8, 2, 1)]


def test_profile_store_replaces_review_snapshot(stores, engine) -> None:
    """v1 리뷰 7·8 → v2 리뷰 8(별점 바뀜)·9: 7은 지워지고 8은 갱신, 9는 새로 (스냅샷 교체). 삭제·upsert 대상이 겹치지 않는다."""
    _, profiles = stores
    profiles.upsert(_prof(1, [_rec(7, 5, 0), _rec(8, 2, 1)]))
    profiles.upsert(_prof(2, [_rec(8, 4, 0), _rec(9, 3, 1)]))
    rows = _review_rows(engine, RID + 3)
    assert [r[:3] for r in rows] == [(8, 4, 0), (9, 3, 1)]
    assert rows[0][3] == review_hash(Review(product_id=8, rating=4))       # 별점이 바뀌면 지문도 바뀐다


def test_profile_store_empty_reviews_clears_children(stores, engine) -> None:
    _, profiles = stores
    profiles.upsert(_prof(1, [_rec(7, 5, 0)]))
    profiles.upsert(_prof(2, []))                                          # 리뷰를 다 지운 사용자
    assert _review_rows(engine, RID + 3) == [] and profiles.get(RID + 3).reviews == []


def test_profile_store_lower_version_touches_neither_table(stores, engine) -> None:
    """늦게 도착한 옛 분석(낮은 번호)은 프로필도 리뷰도 건드리지 않는다 — 가드에 막히면 RETURNING 0행 → 자식 문장도 0행."""
    _, profiles = stores
    profiles.upsert(_prof(5, [_rec(8, 4, 0)], scores=[CategoryScore(category_id=11, n=1, score_sum=1)]))
    profiles.upsert(_prof(4, [_rec(7, 1, 0)], scores=[CategoryScore(category_id=99, n=1, score_sum=-2)]))
    got = profiles.get(RID + 3)
    assert got.source_version == 5 and [r.product_id for r in got.reviews] == [8] and got.category_scores[0].category_id == 11


def test_profile_store_same_version_resend_keeps_values(stores) -> None:
    """같은 번호 재전송 — 가드가 <= 라 다시 쓰지만 값은 그대로 (시각만 바뀔 수 있어 값으로 비교)."""
    _, profiles = stores
    p = _prof(3, [_rec(7, 5, 0), _rec(8, 2, 1)], scores=[CategoryScore(category_id=11, n=2, score_sum=3)], pref="휴대용")
    profiles.upsert(p)
    first = profiles.get(RID + 3)
    profiles.upsert(p)
    again = profiles.get(RID + 3)
    assert again.model_dump(exclude={"updated_at"}) == first.model_dump(exclude={"updated_at"})


def test_profile_store_is_atomic(stores, engine) -> None:
    """리뷰 하나를 DB 가 거부하면(CHECK) 문장 전체가 실패 — 프로필도 이전 그대로. pydantic 검증은 model_construct 로 건너뛴다."""
    _, profiles = stores
    profiles.upsert(_prof(1, [_rec(7, 5, 0)], pref="처음"))
    bad = ReviewRecord.model_construct(product_id=8, rating=5, position=10, review_hash="a" * 64)   # 자리 10 — CHECK 0~9 위반
    with pytest.raises(sa.exc.IntegrityError, match="ck_recipient_reviews_position"):
        profiles.upsert(_prof(2, [_rec(9, 4, 0), bad], pref="나중"))
    got = profiles.get(RID + 3)
    assert got.source_version == 1 and got.gift_preference_hash == preference_hash("처음") and [r.product_id for r in got.reviews] == [7]


def test_profile_store_inside_run_lock_is_one_atomic_statement(stores) -> None:
    """운영 경로 — upsert 는 run_lock 의 잠금 커넥션(AUTOCOMMIT, 문장마다 커밋)에서 돈다. 그래도 한 문장이라 저장은 통째로,
    실패도 통째로(프로필 그대로) — 트랜잭션 세 문장이었다면 여기서 프로필만 바뀌고 리뷰가 빠지는 반쪽 저장이 생긴다."""
    runs, profiles = stores
    with runs.run_lock(RID + 4, 1) as got:
        assert got
        profiles.upsert(_prof(1, [_rec(7, 5, 0)], rid=RID + 4, pref="처음"))
        bad = ReviewRecord.model_construct(product_id=8, rating=0, position=1, review_hash="b" * 64)   # 별점 0 — CHECK 위반
        with pytest.raises(sa.exc.IntegrityError, match="ck_recipient_reviews_rating"):
            profiles.upsert(_prof(2, [_rec(9, 4, 0), bad], rid=RID + 4, pref="나중"))
        profiles.upsert(_prof(3, [_rec(9, 4, 0)], rid=RID + 4))                                          # 실패 뒤에도 같은 커넥션 사용 가능
    got = profiles.get(RID + 4)
    assert got.source_version == 3 and [r.product_id for r in got.reviews] == [9] and got.gift_preference_hash is None


def test_profile_store_delete_cascades_reviews(stores, engine) -> None:
    _, profiles = stores
    profiles.upsert(_prof(1, [_rec(7, 5, 0), _rec(8, 2, 1)]))
    assert profiles.delete(RID + 3) is True
    assert _count(engine, "recipient_reviews", RID + 3) == 0


# ---------------------------------------------------------------- pipeline → 두 테이블


def test_pipeline_writes_both_tables(stores, engine) -> None:
    runs, profiles = stores
    rq = _rq(rid=RID + 1)
    out = pipeline.profile(rq, catalog=FileCatalogReader(FIXTURE), store=runs, recipient_store=profiles)
    assert out.status is RunStatus.RESULT_READY and len(out.search.product_ids) == 30
    run = runs.get(RID + 1)
    assert run.status is RunStatus.RESULT_READY and run.search.product_ids == out.search.product_ids and run.input_hash == out.input_hash
    prof = profiles.get(RID + 1)
    assert prof.source_version == 3 and [(c.category_id, c.category_name) for c in prof.disliked_categories] == [(802, "출산·육아용품")]


def test_pipeline_with_reviews_writes_scores_and_review_rows(stores, engine) -> None:
    """리뷰가 든 요청 한 건 → 실행 기록 · 프로필(점수 · 지문) · 리뷰 행까지 실제 DB 에 들어간다(2·3·4단계 한 줄). 원문은 어디에도 없다."""
    runs, profiles = stores
    rq = ProfileRequest(recipient_user_id=RID + 2, source_version=1, gift_preference="육아용품 좋아요", disliked_categories=[],
                        reviews=[Review(product_id=13006, rating=5, review_text="아기가 좋아해요"), Review(product_id=21416, rating=2)])
    out = pipeline.profile(rq, catalog=FileCatalogReader(FIXTURE), store=runs, recipient_store=profiles)
    assert out.status is RunStatus.RESULT_READY
    prof = profiles.get(RID + 2)
    assert {(s.category_id, s.n, s.score_sum) for s in prof.category_scores} == {(802, 1, 2), (305, 1, -1)}
    assert [r.product_id for r in prof.reviews] == [13006, 21416] and prof.gift_preference_hash == preference_hash("육아용품 좋아요")
    # 원문 그대로는 없고 정리본만(10-13 DR-039) — 이 글들은 가릴 것이 없어 정리본 = 원문이므로 정리본 칸에 그대로 있다
    assert prof.gift_preference_clean == "육아용품 좋아요" and [r.text_clean for r in prof.reviews] == ["아기가 좋아해요", None]
    assert prof.tag_status == "off" and len(prof.recommended_product_ids) == len(out.search.product_ids) and prof.guard_version == "g4"


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
            assert res.status_code == 202
            t0 = time.monotonic()                                            # 09-28: 워커 스레드가 끝내기를 기다린다
            while time.monotonic() - t0 < 10 and not (len(fake.sent) == 1 and (r := app.state.store.get(rid)) and r.status is RunStatus.DELIVERED):
                time.sleep(0.02)
            assert len(fake.sent) == 1

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



def test_delivered_supersedes_older_undelivered_rows(stores) -> None:
    """10-08 실측 S3: 콜백 불통으로 v1 이 RESULT_READY 로 남은 채 v2 가 DELIVERED 되면 v1 은 Backend 가 다시 묻지 않는 번호다.
    save(DELIVERED) 가 그보다 낮은 RESULT_READY 를 SUPERSEDED(CALLBACK_STALE) 로 내려 undelivered 에서 뺀다. 더 높은 번호는 건드리지 않는다."""
    runs, _ = stores

    def ready(sv: int) -> ProfileOutcome:
        return ProfileOutcome(recipient_user_id=RID, source_version=sv, status=RunStatus.RESULT_READY, input_hash=f"h{sv}",
                              search=SearchResult(product_ids=[1], query_text="", catalog_version_id=FILE_CATALOG_VERSION_ID))

    for sv in (1, 2, 3):
        runs.save(ready(sv))
    before = runs.undelivered_count()
    runs.save(ready(2).model_copy(update={"status": RunStatus.DELIVERED, "callback_attempts": 1}))
    v1, v2, v3 = (runs.get_run(RID, sv) for sv in (1, 2, 3))
    assert v1.status is RunStatus.SUPERSEDED and v1.failure_code is ErrorCode.CALLBACK_STALE and "v2" in v1.failure_reason
    assert v2.status is RunStatus.DELIVERED
    assert v3.status is RunStatus.RESULT_READY                      # 더 높은 번호는 그대로 — 아직 전달 대상
    assert runs.undelivered_count() == before - 2                   # v1(대체) · v2(전달) 둘이 빠지고 v3 만 남는다


def test_run_lock_does_not_leave_transaction_open(engine) -> None:
    """이슈 2026-09-27_1633 A — 잠금을 든 커넥션이 작업 내내 `idle in transaction` 이면 안 된다.

    v3(긴 작업)에서 VACUUM 을 막고, idle_in_transaction_session_timeout 에 세션이 끊기면 잠금도 사라져
    이 잠금이 막으려던 중복 분석이 난다. 고치기 전에는 이 질의가 1 을 돌려줬다.
    """
    store = DbProfileRunStore(engine)
    with store.run_lock(991_003, 1) as got:
        assert got is True
        with engine.connect() as c:
            open_tx = c.execute(sa.text(
                "select count(*) from pg_stat_activity "
                "where state = 'idle in transaction' and query like '%pg_try_advisory_lock%'")).scalar_one()
    assert open_tx == 0


def test_profile_runs_column_comments_match_0004(engine) -> None:
    """0004 — 열 주석이 실제 값(ai_catalog)과 updated_at 의 역할을 말하는지. 09-27 점검에서 한 번 어긋났던 것이라 잠근다."""
    with engine.connect() as c:
        comments = dict(c.execute(sa.text("""
            select column_name, col_description('ai_profile.profile_runs'::regclass, ordinal_position)
            from information_schema.columns
            where table_schema = 'ai_profile' and table_name = 'profile_runs'
              and column_name in ('catalog_version_id', 'updated_at')""")).all())
    assert "ai_catalog.catalog_versions.id" in comments["catalog_version_id"]
    assert "ai_search" not in comments["catalog_version_id"]
    assert "RUNNING_STALE_S" in comments["updated_at"]


def test_run_lock_connection_is_reused_for_store_calls(engine) -> None:
    """잠금을 쥔 동안 저장소 호출은 풀에서 더 꺼내지 않고(체크아웃 0) 잠금 커넥션에서 돈다. 나가면 다시 풀에서 꺼낸다.
    AUTOCOMMIT 이라 문장마다 곧바로 커밋된다 — 잠금 밖에서 읽어도 보인다."""
    rid, sv = 991_004, 1
    runs, profiles = DbProfileRunStore(engine), DbRecipientProfileStore(engine)
    checkouts: list[int] = []

    def on_checkout(dbapi_conn, record, proxy) -> None:
        checkouts.append(1)

    sa.event.listen(engine, "checkout", on_checkout)
    try:
        with runs.run_lock(rid, sv) as got:
            assert got is True
            n_lock = len(checkouts)                                          # 잠금 커넥션 하나
            assert runs.get_run(rid, sv) is None
            runs.save(ProfileOutcome(recipient_user_id=rid, source_version=sv, status=RunStatus.RUNNING, input_hash="h"))
            profiles.upsert(RecipientProfile(recipient_user_id=rid, source_version=sv))
            assert len(checkouts) == n_lock                                  # 풀에서 더 꺼내지 않았다
        assert runs.get_run(rid, sv).status is RunStatus.RUNNING            # 커밋돼 있다
        assert profiles.get(rid) is not None
        assert len(checkouts) == n_lock + 2                                  # 잠금 밖 읽기 둘은 풀에서
    finally:
        sa.event.remove(engine, "checkout", on_checkout)
        runs.delete_recipient(rid)
        profiles.delete(rid)


def test_pipeline_with_tagger_stores_chatbot_profile_data(stores, engine) -> None:
    """0006 — 챗봇 조회용 데이터가 DB 까지: 정리본 · 축이 붙은 태그 · 태그 상태 · 판 · 30개, 실행 기록에도 상태 · 판 · 비용(10-13)."""
    from profiling import tagging
    from profiling.ports import TagReply, TokenUsage

    class Tagger:
        model = "fake-llm"

        def extract(self, system, prompt, schema):
            body = '{"tags": [{"axis": "카테고리", "tag": "출산선물세트", "ref": "pref", "evidence": "x"},' \
                   ' {"axis": "특징", "tag": "부드러움", "ref": "pref", "evidence": "x"}]}'
            return TagReply(body, TokenUsage(input_tokens=700, output_tokens=60, cost_usd=0.000412))

    runs, profiles = stores
    t3 = tagging.load_spec("tag3")
    rq = ProfileRequest(recipient_user_id=RID + 3, source_version=2, gift_preference="010-1234-5678 로 연락 주세요 아기 선물 좋아요",
                        disliked_categories=[], reviews=[Review(product_id=13006, rating=5, review_text="ㅋㅋㅋㅋㅋㅋ")])
    out = pipeline.profile(rq, catalog=FileCatalogReader(FIXTURE), store=runs, recipient_store=profiles, tagger=Tagger(), tag_spec=t3)
    assert out.status is RunStatus.RESULT_READY
    prof = profiles.get(RID + 3)
    assert prof.gift_preference_clean == "[연락처] 로 연락 주세요 아기 선물 좋아요" and [r.text_clean for r in prof.reviews] == [None]
    assert [(t.axis, t.tag, t.source, t.ref) for t in prof.preferred_tags] == [("카테고리", "출산선물세트", "preference", "pref")]   # 3축 — 특징은 '축 밖'
    assert prof.tag_status == "ok" and prof.prompt_version == f"{t3.version}@fake-llm" and prof.validator_version == "tag3-v1"
    assert prof.recommended_product_ids == out.search.product_ids and prof.catalog_version_id == out.search.catalog_version_id
    run = runs.get_run(RID + 3, 2)
    assert (run.tag_status, run.prompt_version, run.validator_version) == ("ok", f"{t3.version}@fake-llm", "tag3-v1")
    assert run.llm_cost_usd == pytest.approx(0.000412)
    with engine.connect() as c:
        dumped = c.execute(sa.text("select row_to_json(p)::text from ai_profile.recipient_profiles p where p.recipient_user_id = :rid"),
                           {"rid": RID + 3}).scalar()
    assert "010-1234-5678" not in dumped                                             # 원문의 개인정보는 DB 어디에도 없다
