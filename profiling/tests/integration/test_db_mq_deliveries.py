"""ai_profile.mq_deliveries(0012) + mq.store.DbMqDeliveryStore — 실제 PostgreSQL.

PROFILING_DATABASE_URL 이 없으면 conftest 가 건너뛴다. 이 파일이 만든 행(원본 해시 접두사 'f0f0')만 지운다.
"""
import hashlib
import uuid

import pytest
import sqlalchemy as sa

from profiling.mq import store as st
from profiling.settings import get_settings
from profiling.stores import DbProfileRunStore

PREFIX = "f0f0"
RID = "8a3851f6-1a29-4f44-a812-f6d05ad3d8e1"
RID2 = "0b5c2b1e-7e0a-4b0e-9a55-2f2f6a3f9c11"
UID = 990_000_101


def _sha(tag: str) -> str:
    return PREFIX + hashlib.sha256(tag.encode()).hexdigest()[4:]


@pytest.fixture(scope="module")
def engine():
    eng = sa.create_engine(get_settings().DATABASE_URL, future=True)
    try:
        with eng.connect() as c:
            c.execute(sa.text("select 1 from ai_profile.mq_deliveries limit 1"))
    except sa.exc.SQLAlchemyError as e:
        pytest.skip(f"mq_deliveries 를 읽을 수 없음(0012 미적용?): {type(e).__name__}")
    yield eng
    eng.dispose()


@pytest.fixture
def ds(engine):
    def wipe():
        with engine.begin() as c:
            c.execute(sa.text("delete from ai_profile.mq_deliveries where request_body_sha256 like :p"), {"p": PREFIX + "%"})
    wipe()
    yield st.DbMqDeliveryStore(engine)
    wipe()


def _identify(ds, row, rid=RID, uid=UID, sv=7, ih="h" * 64):
    ds.set_identity(row.delivery_id, request_id=rid, recipient_user_id=uid, source_version=sv, input_hash=ih,
                    converted={"requestId": rid, "payload": {"giftPreference": "정리본"}})


def test_접수는_원본_해시로_한_행(ds):
    a = ds.receive(_sha("a"))
    b = ds.receive(_sha("a"))
    assert a.delivery_id == b.delivery_id and a.state == st.RECEIVED and a.analysis_attempts == 0


def test_식별자는_처음_값을_바꾸지_않음_변환본_저장(ds, engine):
    r = ds.receive(_sha("a"))
    _identify(ds, r)
    _identify(ds, r, sv=99)
    g = ds.get(r.delivery_id)
    assert (g.request_id, g.recipient_user_id, g.source_version) == (RID, UID, 7)
    with engine.connect() as c:
        conv, hv = c.execute(sa.text("select request_converted, hash_version from ai_profile.mq_deliveries where delivery_id = :i"),
                             {"i": r.delivery_id}).one()
    assert conv["payload"]["giftPreference"] == "정리본" and hv == st.HASH_VERSION


def test_바인딩은_requestId_하나_수신자버전_하나(ds):
    a, b = ds.receive(_sha("a")), ds.receive(_sha("b"))
    _identify(ds, a)
    _identify(ds, b)                                            # 같은 요청의 다른 원본
    ds.bind(a.delivery_id)
    with pytest.raises(sa.exc.IntegrityError):
        ds.bind(b.delivery_id)
    assert [x.delivery_id for x in ds.binding_for(RID, UID, 7)] == [a.delivery_id]
    assert [x.delivery_id for x in ds.binding_for(RID2, UID, 7)] == [a.delivery_id]   # 같은 (수신자, 버전) 로도 찾음


def test_식별자_없이_바인딩_불가(ds):
    r = ds.receive(_sha("a"))
    with pytest.raises(sa.exc.IntegrityError):
        ds.bind(r.delivery_id)


def test_시도_횟수는_커밋되고_누적(ds):
    r = ds.receive(_sha("a"))
    assert ds.start_attempt(r.delivery_id) == 1 and ds.start_attempt(r.delivery_id) == 2
    assert ds.get(r.delivery_id).state == st.RUNNING


def test_결과_바이트는_처음_것만_트리거가_변경을_막음(ds, engine):
    r = ds.receive(_sha("a"))
    assert ds.set_result(r.delivery_id, b"first") == b"first"
    assert ds.set_result(r.delivery_id, b"second") == b"first"
    with pytest.raises(sa.exc.DBAPIError), engine.begin() as c:
        c.execute(sa.text("update ai_profile.mq_deliveries set result_body = 'x' where delivery_id = :i"), {"i": r.delivery_id})
    assert ds.get(r.delivery_id).state == st.RESULT_READY


def test_SETTLED_는_발행_확인_뒤에만(ds):
    r = ds.receive(_sha("a"))
    ds.set_result(r.delivery_id, b"x")
    with pytest.raises(sa.exc.IntegrityError):
        ds.settle(r.delivery_id)
    ds.mark_result_confirmed(r.delivery_id)
    ds.settle(r.delivery_id)
    assert ds.get(r.delivery_id).state == st.SETTLED


def test_DLQ_가_필요하면_DLQ_확인_뒤에만_SETTLED(ds):
    r = ds.receive(_sha("a"))
    ds.mark_dlq_required(r.delivery_id, "UNIDENTIFIABLE")
    with pytest.raises(sa.exc.IntegrityError):
        ds.settle(r.delivery_id)
    ds.mark_dlq_confirmed(r.delivery_id)
    ds.settle(r.delivery_id)
    assert ds.get(r.delivery_id).failure_code == "UNIDENTIFIABLE"


def test_requestId_잠금은_한_곳만(ds):
    with ds.request_lock(RID) as a:
        with ds.request_lock(RID) as b:
            assert a is True and b is False
        with ds.request_lock(RID2) as c:
            assert c is True
    with ds.request_lock(RID) as again:
        assert again is True


def test_run_lock_안에서도_저장소가_같은_커넥션으로_동작(ds, engine):
    runs = DbProfileRunStore(engine)
    r = ds.receive(_sha("a"))
    _identify(ds, r)
    with ds.request_lock(RID) as g1, runs.run_lock(UID, 7, strict=True) as g2:
        assert g1 and g2
        ds.bind(r.delivery_id)
        assert ds.start_attempt(r.delivery_id) == 1
        assert ds.set_result(r.delivery_id, b"x") == b"x"
    assert ds.get(r.delivery_id).is_binding


def test_앱_계정이_읽고_쓸_수_있음(engine):
    with engine.connect() as c:
        roles = [x for (x,) in c.execute(sa.text("select rolname from pg_roles where rolname = any(:r)"), {"r": ["ai_app"]})]
        if not roles:
            pytest.skip("앱 계정 없음(로컬 compose 아님)")
        ok = c.execute(sa.text("select has_table_privilege('ai_app', 'ai_profile.mq_deliveries', 'select, insert, update')")).scalar_one()
    assert ok


def test_uuid_형_반환(ds):
    assert isinstance(ds.receive(_sha("a")).delivery_id, uuid.UUID)


# ---------------------------------------------------------------- 처리 흐름 + 실제 DB 잠금(분석 · 발행은 가짜)
def test_처리_흐름_실제_DB(ds, engine):
    import json

    from profiling.mq import process as pr
    from profiling.mq.consumer import Delivery
    from profiling.mq.messages import parse_request
    from profiling.mq.publisher import PublishResult
    from profiling.types import ProfileOutcome, RunStatus, SearchResult

    body = json.dumps({"schemaVersion": 1, "requestId": RID, "createdAt": "2026-11-07T08:00:00Z",
                       "payload": {"recipientUserId": UID, "sourceVersion": 7, "dislikedCategories": [], "giftPreference": "커피",
                                   "reviews": []}}, ensure_ascii=False).encode()
    sha = parse_request(body, RID).body_sha256
    with engine.begin() as c:                                    # 이 시험의 원본 해시는 접두사가 달라 직접 지운다
        c.execute(sa.text("delete from ai_profile.mq_deliveries where request_body_sha256 = :s"), {"s": sha})

    sent: list[bytes] = []

    class Pub:
        def publish(self, rk, b, *, message_id, correlation_id=None, headers=None):
            sent.append(b)
            return PublishResult.CONFIRMED

    class Ctl:
        stopping = False

        def pause(self, r): ...
        def resume(self, r): ...
        def wait(self, s): return True

    calls = []

    def analyze(rq):
        calls.append(1)
        return ProfileOutcome(recipient_user_id=rq.recipient_user_id, source_version=rq.source_version, status=RunStatus.RESULT_READY,
                              input_hash="x", search=SearchResult(product_ids=[4, 2], query_text="", catalog_version_id=uuid.UUID(int=1)))

    proc = pr.Processor(deliveries=ds, runs=DbProfileRunStore(engine), analyze=analyze, publisher=Pub(), control=Ctl(),
                        max_attempts=3, wait_s=0.01, retry_base_s=1, retry_max_s=2)
    acks = []
    try:
        for _ in range(2):                                       # 두 번째는 재배달 — 재분석 없음
            d = Delivery(body=body, message_id=RID, delivery_tag=1, redelivered=False, _ack=lambda: acks.append(1) or True)
            assert proc.handle(d) is pr.Handled.ACKED
        assert calls == [1] and acks == [1, 1] and sent[0] == sent[1]
        row = ds.receive(sha)
        assert row.state == st.SETTLED and row.result_confirmed and row.is_binding
    finally:
        with engine.begin() as c:
            c.execute(sa.text("delete from ai_profile.mq_deliveries where request_body_sha256 = :s"), {"s": sha})


# ---------------------------------------------------------------- 6 · 7 · 8 (실제 DB)
def test_원본_잠금은_한_곳만(ds):
    with ds.body_lock(_sha("x")) as a, ds.body_lock(_sha("x")) as b:
        assert a is True and b is False


def test_requestId_잠금_커넥션이_끊기면_확인에서_예외(ds, engine):
    ds.check_request_lock()                                      # 잠금 밖 — 아무것도 안 함
    with ds.request_lock(RID) as got:
        assert got
        ds.check_request_lock()                                  # 살아 있음
        pid = st._req_local.conn.execute(sa.text("select pg_backend_pid()")).scalar_one()
        with engine.connect() as c:
            c.execute(sa.text("select pg_terminate_backend(:p)"), {"p": pid})
        with pytest.raises(sa.exc.DBAPIError):
            ds.check_request_lock()
    with ds.request_lock(RID) as again:                          # 끊긴 커넥션은 풀에 돌아가 잠금을 막지 않는다
        assert again is True


def test_재시도_대기_와_상태_집계(ds):
    from datetime import UTC, datetime, timedelta
    a, b = ds.receive(_sha("a")), ds.receive(_sha("b"))
    at = datetime.now(UTC) + timedelta(minutes=5)
    ds.set_retry_wait(a.delivery_id, at)
    ds.mark_dlq_required(b.delivery_id, "UNIDENTIFIABLE")
    g = ds.get(a.delivery_id)
    assert g.state == st.RETRY_WAIT and abs((g.next_attempt_at - at).total_seconds()) < 1
    h = ds.health_counts()
    assert h["open"] >= 2 and h["retry_wait"] >= 1 and h["dlq_pending"] >= 1 and h["oldest_open_s"] >= 0


def test_처리_흐름_실제_DB_충돌은_실패결과_실패큐_SETTLED(ds, engine):
    import json

    from profiling.mq import process as pr
    from profiling.mq.consumer import Delivery
    from profiling.mq.messages import parse_request
    from profiling.mq.publisher import PublishResult
    from profiling.types import ProfileOutcome, RunStatus, SearchResult

    def body(rid, pref):
        return json.dumps({"schemaVersion": 1, "requestId": rid, "createdAt": "2026-11-07T08:00:00Z",
                           "payload": {"recipientUserId": UID, "sourceVersion": 7, "dislikedCategories": [], "giftPreference": pref,
                                       "reviews": []}}, ensure_ascii=False).encode()
    b1, b2 = body(RID, "커피"), body(RID2, "와인")
    shas = [parse_request(b1, RID).body_sha256, parse_request(b2, RID2).body_sha256]
    runs = DbProfileRunStore(engine)

    def wipe():
        with engine.begin() as c:
            c.execute(sa.text("delete from ai_profile.mq_deliveries where request_body_sha256 = any(:s)"), {"s": shas})
        runs.delete_recipient(UID)
    wipe()
    sent = []

    class Pub:
        def publish(self, rk, b, *, message_id, correlation_id=None, headers=None):
            sent.append((rk, b, headers))
            return PublishResult.CONFIRMED

    class Ctl:
        stopping = False

        def pause(self, r): ...
        def resume(self, r): ...
        def wait(self, s): return False

    def analyze(rq):
        return ProfileOutcome(recipient_user_id=rq.recipient_user_id, source_version=rq.source_version, status=RunStatus.RESULT_READY,
                              input_hash="x", search=SearchResult(product_ids=[4, 2], query_text="", catalog_version_id=uuid.UUID(int=1)))

    proc = pr.Processor(deliveries=ds, runs=runs, analyze=analyze, publisher=Pub(), control=Ctl(), max_attempts=3, wait_s=0.01,
                        retry_base_s=1, retry_max_s=2)
    try:
        for b, mid in ((b1, RID), (b2, RID2)):
            d = Delivery(body=b, message_id=mid, delivery_tag=1, redelivered=False, _ack=lambda: True)
            assert proc.handle(d) is pr.Handled.ACKED
        assert [s[0] for s in sent] == ["profiling.result", "profiling.result", "profiling.request.failed"]
        assert json.loads(sent[1][1])["payload"]["error"]["code"] == "REQUEST_CONFLICT" and sent[2][1] == b2
        row = ds.receive(shas[1])
        assert row.state == st.SETTLED and row.dlq_confirmed and row.failure_code == "REQUEST_CONFLICT" and not row.is_binding
    finally:
        wipe()


def test_대조_복구_실제_DB(ds, engine):
    from profiling.mq import reconcile as rc
    from profiling.mq.publisher import PublishResult

    old, fresh, wait = ds.receive(_sha("old")), ds.receive(_sha("fresh")), ds.receive(_sha("wait"))
    for r in (old, fresh, wait):
        _identify(ds, r, rid=str(uuid.uuid4()))
    ds.set_result(old.delivery_id, b"R")
    ds.set_result(fresh.delivery_id, b"R")
    with engine.begin() as c:
        c.execute(sa.text("update ai_profile.mq_deliveries set updated_at = now() - interval '2 hours' where delivery_id = any(:i)"),
                  {"i": [old.delivery_id, wait.delivery_id]})
        c.execute(sa.text("update ai_profile.mq_deliveries set state = 'RETRY_WAIT', next_attempt_at = now() + interval '1 minute' "
                          "where delivery_id = :i"), {"i": wait.delivery_id})
    ids = {r.delivery_id for r in ds.stale_open(3600, 100)}
    assert old.delivery_id in ids and fresh.delivery_id not in ids and wait.delivery_id not in ids   # 재시도 대기 중은 오래됨 아님
    sent = []

    class Pub:
        def publish(self, rk, b, *, message_id, correlation_id=None, headers=None):
            sent.append(b)
            return PublishResult.CONFIRMED
    rep = rc.Reconciler(ds, Pub(), older_than_s=3600).run_once()
    assert rep.counts.get("REPUBLISHED", 0) >= 1 and b"R" in sent
    g = ds.get(old.delivery_id)
    assert g.state == st.SETTLED and g.result_confirmed
