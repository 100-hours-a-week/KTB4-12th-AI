"""mq.process — 잠금 안 판정(decide) · 처리 순서(접수 → 잠금 → 분석 → 결과 저장 → 발행 → SETTLED → ACK).

임시 목(mock): 메모리 전달 기록(DbMqDeliveryStore 와 같은 규칙) · 가짜 profile_runs · 가짜 발행기 · 가짜 소비자 제어 · 가짜 분석.
브로커 · DB · LLM 을 부르지 않는다(비용 0).
"""
import json
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from profiling.mq import process as pr
from profiling.mq import store as st
from profiling.mq.consumer import Delivery
from profiling.mq.publisher import PublishResult
from profiling.types import ErrorCode, ProfileOutcome, RunStatus, SearchResult

RID = "8a3851f6-1a29-4f44-a812-f6d05ad3d8e1"
RID2 = "0b5c2b1e-7e0a-4b0e-9a55-2f2f6a3f9c11"
NOW = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)
CAT = uuid.UUID(int=1)


def _body(rid=RID, uid=42, sv=7, pref="커피", created="2026-11-07T08:00:00Z") -> bytes:
    return json.dumps({"schemaVersion": 1, "requestId": rid, "createdAt": created,
                       "payload": {"recipientUserId": uid, "sourceVersion": sv, "dislikedCategories": [],
                                   "giftPreference": pref, "reviews": []}}, ensure_ascii=False).encode()


# ---------------------------------------------------------------- 임시 목
class MemDeliveries:
    """DbMqDeliveryStore 와 같은 규칙(원본 해시 upsert · coalesce · 결과 바이트 불변 · SETTLED 조건)을 메모리로."""

    def __init__(self):
        self.rows: dict[uuid.UUID, st.DeliveryRow] = {}
        self.held: set[str] = set()
        self.fail_next = 0                     # >0 이면 그 횟수만큼 다음 호출에서 DB 오류
        self.lock_lost = 0                     # >0 이면 그 횟수만큼 check_request_lock 이 '잠금 커넥션 끊김'
        self.calls: list[str] = []
        self.body_locks: list[str] = []
        self.retry_waits: list[tuple] = []

    def _db(self, name):
        self.calls.append(name)
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("db down")

    def receive(self, sha):
        self._db("receive")
        for r in self.rows.values():
            if r.request_body_sha256 == sha:
                return r
        r = st.DeliveryRow(uuid.uuid4(), sha, None, None, None, None, False, st.RECEIVED, 0, None, None, False, False, False, None)
        self.rows[r.delivery_id] = r
        return r

    def get(self, i):
        self._db("get")
        return self.rows[i]

    def _upd(self, i, **kw):
        self.rows[i] = replace(self.rows[i], **kw)

    def set_identity(self, i, *, request_id, recipient_user_id, source_version, input_hash, converted):
        self._db("set_identity")
        r = self.rows[i]
        self._upd(i, request_id=r.request_id or request_id, recipient_user_id=r.recipient_user_id or recipient_user_id,
                  source_version=r.source_version or source_version, input_hash=r.input_hash or input_hash)

    @contextmanager
    def request_lock(self, request_id):
        self._db("request_lock")
        got = request_id not in self.held
        if got:
            self.held.add(request_id)
        try:
            yield got
        finally:
            if got:
                self.held.discard(request_id)

    @contextmanager
    def body_lock(self, sha):
        self._db("body_lock")
        self.body_locks.append(sha)
        got = sha not in self.held
        if got:
            self.held.add(sha)
        try:
            yield got
        finally:
            if got:
                self.held.discard(sha)

    def check_request_lock(self):
        self._db("check_request_lock")
        if self.lock_lost:
            self.lock_lost -= 1
            raise ConnectionError("lock connection lost")

    def binding_for(self, request_id, uid, sv):
        self._db("binding_for")
        return [r for r in self.rows.values() if r.is_binding and (r.request_id == request_id or (r.recipient_user_id, r.source_version) == (uid, sv))]

    def bind(self, i):
        self._db("bind")
        others = [r for r in self.rows.values() if r.is_binding and r.delivery_id != i]
        me = self.rows[i]
        assert not any(o.request_id == me.request_id or (o.recipient_user_id, o.source_version) == (me.recipient_user_id, me.source_version)
                       for o in others), "부분 UNIQUE 위반"
        self._upd(i, is_binding=True)

    def start_attempt(self, i):
        self._db("start_attempt")
        self._upd(i, state=st.RUNNING, analysis_attempts=self.rows[i].analysis_attempts + 1, next_attempt_at=None)
        return self.rows[i].analysis_attempts

    def set_result(self, i, body, *, failure_code=None, dlq_required=False):
        self._db("set_result")
        if self.rows[i].result_body is None:
            self._upd(i, result_body=body, failure_code=failure_code, dlq_required=dlq_required, state=st.RESULT_READY)
        return self.rows[i].result_body

    def set_retry_wait(self, i, at):
        self._db("set_retry_wait")
        self.retry_waits.append((i, at))
        self._upd(i, state=st.RETRY_WAIT, next_attempt_at=at)

    def mark_dlq_required(self, i, code):
        self._db("mark_dlq_required")
        self._upd(i, dlq_required=True, failure_code=self.rows[i].failure_code or code)

    def mark_dlq_confirmed(self, i):
        self._db("mark_dlq_confirmed")
        assert self.rows[i].dlq_required, "ck_mq_deliveries_dlq_confirmed"
        self._upd(i, dlq_confirmed=True)

    def mark_result_confirmed(self, i):
        self._db("mark_result_confirmed")
        assert self.rows[i].result_body is not None
        self._upd(i, result_confirmed=True)

    def settle(self, i):
        self._db("settle")
        r = self.rows[i]
        assert (r.result_body is None or r.result_confirmed) and (not r.dlq_required or r.dlq_confirmed), "ck_mq_deliveries_settled"
        self._upd(i, state=st.SETTLED)


class MemRuns:
    def __init__(self):
        self.runs: dict[tuple, ProfileOutcome] = {}
        self.held: set[tuple] = set()
        self.strict_seen: list[bool] = []

    @contextmanager
    def run_lock(self, uid, sv, *, strict=False):
        self.strict_seen.append(strict)
        got = (uid, sv) not in self.held
        if got:
            self.held.add((uid, sv))
        try:
            yield got
        finally:
            if got:
                self.held.discard((uid, sv))

    def get_run(self, uid, sv):
        return self.runs.get((uid, sv))


class FakePublisher:
    def __init__(self, script=()):
        self.script, self.sent = list(script), []

    def publish(self, routing_key, body, *, message_id, correlation_id=None, headers=None):
        self.sent.append((routing_key, body, message_id, correlation_id, headers))
        return self.script.pop(0) if self.script else PublishResult.CONFIRMED


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


class FakeControl:
    """RequestConsumer 의 제어 대역 — 멈춤은 이유별 개수, wait 는 시계를 그만큼 앞으로 돌린다(실제로 잠들지 않음)."""

    def __init__(self, clock=None, stop_after_waits=None):
        self.stopping, self.pauses, self.waits, self.events = False, {}, 0, []
        self.clock, self.stop_after_waits, self.on_wait = clock, stop_after_waits, None

    @property
    def paused(self):
        return sorted(k for k, v in self.pauses.items() if v)

    def pause(self, reason):
        self.pauses[reason] = self.pauses.get(reason, 0) + 1
        self.events.append(("pause", reason))

    def resume(self, reason):
        self.pauses[reason] = max(self.pauses.get(reason, 0) - 1, 0)
        self.events.append(("resume", reason))

    def wait(self, seconds):
        self.waits += 1
        if self.clock is not None:
            self.clock.t += timedelta(seconds=seconds)
        if self.on_wait:
            self.on_wait()
        if self.stop_after_waits is not None and self.waits >= self.stop_after_waits:
            self.stopping = True
        return self.stopping


class Analyzer:
    """가짜 분석(LLM 없음). script 가 있으면 앞에서부터 (상태, 실패 코드)를 하나씩 쓰고, 다 쓰면 기본(성공)."""

    def __init__(self, ids=(5, 3, 9), status=RunStatus.RESULT_READY, code=None, runs=None):
        self.ids, self.status, self.code, self.runs, self.calls = list(ids), status, code, runs, 0
        self.script: list[tuple] = []

    def __call__(self, rq):
        self.calls += 1
        status, code = self.script.pop(0) if self.script else (self.status, self.code)
        o = ProfileOutcome(recipient_user_id=rq.recipient_user_id, source_version=rq.source_version, status=status,
                           input_hash="x", failure_code=code,
                           search=SearchResult(product_ids=self.ids, query_text="", catalog_version_id=CAT) if status is RunStatus.RESULT_READY else None)
        return o


def _delivery(body, mid=RID, acks=None, ok=True):
    acks = acks if acks is not None else []

    def ack():
        acks.append(1)
        return ok
    return Delivery(body=body, message_id=mid, delivery_tag=1, redelivered=False, _ack=ack), acks


@pytest.fixture
def env():
    clock = Clock()
    d, r, p, c = MemDeliveries(), MemRuns(), FakePublisher(), FakeControl(clock)
    a = Analyzer()
    proc = pr.Processor(deliveries=d, runs=r, analyze=a, publisher=p, control=c, max_attempts=3, wait_s=0.01,
                        retry_base_s=10, retry_max_s=60, now=clock, rand=lambda: 0.5)
    return proc, d, r, p, c, a


# ---------------------------------------------------------------- 정상 흐름
def test_정상_요청은_분석_결과저장_발행_SETTLED_ACK(env):
    proc, d, r, p, _c, a = env
    dl, acks = _delivery(_body())
    assert proc.handle(dl) is pr.Handled.ACKED and acks == [1] and a.calls == 1
    (row,) = d.rows.values()
    assert row.is_binding and row.state == st.SETTLED and row.result_confirmed and row.analysis_attempts == 1
    rk, body, mid, cid, headers = p.sent[0]
    assert rk == "profiling.result" and mid == cid == RID and body == row.result_body and headers is None
    assert len(p.sent) == 1                                            # 성공은 실패 큐로 복사하지 않는다
    doc = json.loads(body)
    assert doc["payload"] == {"recipientUserId": 42, "sourceVersion": 7, "outcome": "SUCCEEDED", "data": {"recommendedProductIds": [5, 3, 9]}}
    assert r.strict_seen == [True]                                     # MQ 경로는 run_lock(strict=True)
    # 순서: 결과 저장 → 확인 기록 → SETTLED (ACK 는 그 뒤)
    i = d.calls
    assert i.index("set_result") < i.index("mark_result_confirmed") < i.index("settle")


def test_같은_원본_재배달은_재분석_없이_같은_바이트_재발행(env):
    proc, _d, _r, p, _c, a = env
    proc.handle(_delivery(_body())[0])
    assert proc.handle(_delivery(_body())[0]) is pr.Handled.ACKED
    assert a.calls == 1 and p.sent[0][1] == p.sent[1][1]


def test_같은_요청의_다른_원본은_바인딩_결과_바이트를_그대로(env):
    proc, d, _r, p, _c, a = env
    proc.handle(_delivery(_body())[0])
    assert proc.handle(_delivery(_body(created="2026-11-07T09:00:00Z"))[0]) is pr.Handled.ACKED
    assert a.calls == 1 and len(d.rows) == 2 and p.sent[0][1] == p.sent[1][1]     # createdAt 도 처음 그대로


def test_profile_runs_에_같은_입력_결과가_있으면_재검색_없이_재사용(env):
    proc, d, r, p, _c, a = env
    from profiling.mq.messages import parse_request
    ih = parse_request(_body(), RID).input_hash
    r.runs[(42, 7)] = ProfileOutcome(recipient_user_id=42, source_version=7, status=RunStatus.RESULT_READY, input_hash=ih,
                                     search=SearchResult(product_ids=[8, 1], query_text="", catalog_version_id=CAT))
    assert proc.handle(_delivery(_body())[0]) is pr.Handled.ACKED
    assert a.calls == 0 and json.loads(p.sent[0][1])["payload"]["data"]["recommendedProductIds"] == [8, 1]
    assert next(iter(d.rows.values())).analysis_attempts == 0              # 분석을 안 했으니 예산도 안 씀


def test_profile_runs_결과라도_입력이_다르면_재사용_안_함(env):
    proc, _d, r, _p, _c, a = env
    r.runs[(42, 7)] = ProfileOutcome(recipient_user_id=42, source_version=7, status=RunStatus.DELIVERED, input_hash="other",
                                     search=SearchResult(product_ids=[8], query_text="", catalog_version_id=CAT))
    proc.handle(_delivery(_body())[0])
    assert a.calls == 1


def test_중단된_RUNNING_은_기록된_시도로_이어서(env):
    proc, d, _r, _p, _c, a = env
    proc.handle(_delivery(_body())[0])
    (row,) = d.rows.values()
    d.rows[row.delivery_id] = replace(row, state=st.RUNNING, result_body=None, result_confirmed=False)   # 결과 저장 전 죽은 것처럼
    proc.handle(_delivery(_body())[0])
    assert a.calls == 2 and d.rows[row.delivery_id].analysis_attempts == 2


# ---------------------------------------------------------------- 잠금 · DB · 발행 실패
def test_잠금을_못_얻으면_ACK_안_하고_간격_대기_후_다시(env):
    proc, _d, r, _p, c, a = env
    r.held.add((42, 7))                                        # 다른 실행이 (수신자, 버전) 을 쥐고 있음
    c.stop_after_waits = 3
    dl, acks = _delivery(_body())
    assert proc.handle(dl) is pr.Handled.STOPPED and acks == [] and a.calls == 0 and c.waits == 3
    assert c.paused == []                                      # 잠금 대기는 소비를 멈추지 않는다


def test_잠금이_풀리면_이어서_처리(env):
    proc, d, _r, _p, c, _a = env
    d.held.add(RID)
    orig = c.wait

    def wait(s):
        d.held.discard(RID)                                    # 첫 대기 뒤 풀림
        return orig(s)
    c.wait = wait
    assert proc.handle(_delivery(_body())[0]) is pr.Handled.ACKED and c.waits == 1


def test_DB_장애면_분석_ACK_안_하고_소비_멈춤_복구되면_재개(env):
    proc, d, _r, _p, c, a = env
    d.fail_next = 2
    dl, acks = _delivery(_body())
    assert proc.handle(dl) is pr.Handled.ACKED and acks == [1]
    assert ("pause", "db") in c.events and c.events[-1] == ("resume", "db") and c.waits == 2 and a.calls == 1
    assert c.events.count(("pause", "db")) == 1                                # 같은 처리에서 같은 이유로 두 번 걸지 않는다


def test_DB_장애가_계속되고_종료되면_ACK_없이_STOPPED(env):
    proc, d, _r, _p, c, a = env
    d.fail_next = 99
    c.stop_after_waits = 2
    dl, acks = _delivery(_body())
    assert proc.handle(dl) is pr.Handled.STOPPED and acks == [] and a.calls == 0


def test_분석이_STORE_FAILED_면_DB_장애로_보고_최종_실패로_바꾸지_않음(env):
    proc, _d, _r, _p, c, a = env
    a.status, a.code = RunStatus.FAILED, ErrorCode.STORE_FAILED
    c.stop_after_waits = 1
    dl, acks = _delivery(_body())
    assert proc.handle(dl) is pr.Handled.STOPPED and ("pause", "db") in c.events and acks == []


@pytest.mark.parametrize("res", [PublishResult.RETURNED, PublishResult.NACKED, PublishResult.UNKNOWN])
def test_발행_실패는_결과_유지_소비_멈춤_같은_바이트_재발행(env, res):
    proc, _d, _r, p, c, a = env
    p.script = [res, res]
    dl, acks = _delivery(_body())
    assert proc.handle(dl) is pr.Handled.ACKED and acks == [1]
    assert a.calls == 1 and len(p.sent) == 3 and len({s[1] for s in p.sent}) == 1      # 재분석 없이 같은 바이트
    assert ("pause", "publish") in c.events and c.events[-1] == ("resume", "publish") and c.paused == []


def test_발행_실패_중_종료면_ACK_없이_결과는_남음(env):
    proc, d, _r, p, c, _a = env
    p.script = [PublishResult.UNKNOWN] * 5
    c.stop_after_waits = 1
    dl, acks = _delivery(_body())
    assert proc.handle(dl) is pr.Handled.STOPPED and acks == [] and c.paused == []   # 종료로 끝나도 건 멈춤은 푼다
    (row,) = d.rows.values()
    assert row.result_body is not None and not row.result_confirmed and row.state == st.RESULT_READY


def test_ACK_를_못_보내면_ACK_LOST(env):
    proc, *_ = env
    dl, _ = _delivery(_body(), ok=False)
    assert proc.handle(dl) is pr.Handled.ACK_LOST


# ---------------------------------------------------------------- 6. 최종 실패 · 실패 큐 · 재시도 대기
def _sent(p, rk):
    return [s for s in p.sent if s[0] == rk]


def test_같은_수신자_버전에_다른_입력이면_충돌_실패결과_실패큐_ACK(env):
    proc, d, _r, p, _c, a = env
    proc.handle(_delivery(_body())[0])
    body2 = _body(rid=RID2, pref="와인")
    dl, acks = _delivery(body2, mid=RID2)
    assert proc.handle(dl) is pr.Handled.ACKED and acks == [1] and a.calls == 1          # 재분석 없음
    res = json.loads(_sent(p, "profiling.result")[-1][1])
    assert res["requestId"] == RID2 and res["payload"]["outcome"] == "FAILED" and res["payload"]["error"]["code"] == "REQUEST_CONFLICT"
    (dlq,) = _sent(p, "profiling.request.failed")
    assert dlq[1] == body2 and dlq[2] == RID2                                              # 원본 바이트 그대로 · 원본 message_id
    assert dlq[4]["failure_code"] == "REQUEST_CONFLICT" and "request_body_sha256" in dlq[4] and dlq[4]["original_message_id"] == RID2
    row = next(r for r in d.rows.values() if r.request_id == RID2)
    assert row.state == st.SETTLED and row.result_confirmed and row.dlq_required and row.dlq_confirmed and not row.is_binding
    # 순서: 결과 확인 기록 → 실패 큐 확인 기록 → SETTLED
    i = [c for c in d.calls if c in ("mark_result_confirmed", "mark_dlq_confirmed", "settle")][-3:]
    assert i == ["mark_result_confirmed", "mark_dlq_confirmed", "settle"]


def test_같은_requestId_에_다른_입력이면_충돌(env):
    proc, _d, _r, p, _c, _a = env
    proc.handle(_delivery(_body())[0])
    assert proc.handle(_delivery(_body(sv=8))[0]) is pr.Handled.ACKED
    assert json.loads(p.sent[-2][1])["payload"]["error"]["code"] == "REQUEST_CONFLICT"


def test_식별_불가는_결과_없이_실패큐만_원본_message_id_그대로(env):
    proc, d, _r, p, _c, _a = env
    dl, acks = _delivery(b"{not json", mid="broken-id")
    assert proc.handle(dl) is pr.Handled.ACKED and acks == [1]
    assert _sent(p, "profiling.result") == []                                             # 실패 결과를 지어내지 않는다
    (dlq,) = _sent(p, "profiling.request.failed")
    assert dlq[1] == b"{not json" and dlq[2] == "broken-id" and dlq[4]["failure_code"] == "UNIDENTIFIABLE"
    (row,) = d.rows.values()
    assert row.state == st.SETTLED and row.dlq_confirmed and row.result_body is None and row.request_id is None
    assert d.body_locks == [row.request_body_sha256]                                       # requestId 대신 원본 해시로 잠금


def test_식별_불가_message_id_없음이면_헤더에도_없음(env):
    proc, _d, _r, p, _c, _a = env
    proc.handle(_delivery(b"[]", mid=None)[0])
    (dlq,) = _sent(p, "profiling.request.failed")
    assert dlq[2] is None and "original_message_id" not in dlq[4]


@pytest.mark.parametrize("body, code", [
    (json.dumps({"schemaVersion": 2, "requestId": RID, "createdAt": "2026-11-07T08:00:00Z",
                 "payload": {"recipientUserId": 42, "sourceVersion": 7}}).encode(), "UNSUPPORTED_SCHEMA_VERSION"),
    (json.dumps({"schemaVersion": 1, "requestId": RID, "createdAt": "2026-11-07T08:00:00Z",
                 "payload": {"recipientUserId": 42, "sourceVersion": 7, "dislikedCategories": "x", "giftPreference": None,
                             "reviews": []}}).encode(), "INVALID_REQUEST"),
])
def test_식별되는_거부는_실패결과_와_실패큐(env, body, code):
    proc, d, _r, p, _c, a = env
    assert proc.handle(_delivery(body)[0]) is pr.Handled.ACKED and a.calls == 0
    res = json.loads(_sent(p, "profiling.result")[0][1])
    assert res["payload"]["error"]["code"] == code and res["payload"]["recipientUserId"] == 42
    assert _sent(p, "profiling.request.failed")[0][4]["failure_code"] == code
    (row,) = d.rows.values()
    assert row.state == st.SETTLED and row.failure_code == code and not row.is_binding


def test_분석_일시_실패는_RETRY_WAIT_후_다시_성공(env):
    proc, d, _r, p, c, a = env
    a.script = [(RunStatus.FAILED, ErrorCode.NO_ACTIVE_CATALOG)]                         # 첫 시도만 실패
    dl, acks = _delivery(_body())
    assert proc.handle(dl) is pr.Handled.ACKED and acks == [1] and a.calls == 2
    (row,) = d.rows.values()
    assert row.analysis_attempts == 2 and json.loads(p.sent[0][1])["payload"]["outcome"] == "SUCCEEDED"
    assert d.retry_waits[0][1] == NOW + timedelta(seconds=7.5)                             # 기준 10초 × 2^0 → 절반 5 + 무작위(0.5) × 5
    assert c.paused == []                                                                  # 재시도 대기는 소비를 멈추지 않는다


def test_분석_일시_실패가_예산만큼_이어지면_최종_RETRY_EXHAUSTED(env):
    proc, d, _r, p, _c, a = env
    a.script = [(RunStatus.FAILED, ErrorCode.PIPELINE_ERROR)] * 3
    assert proc.handle(_delivery(_body())[0]) is pr.Handled.ACKED and a.calls == 3
    assert [round((at - NOW).total_seconds(), 1) for _i, at in d.retry_waits][:1] == [7.5]
    assert len(d.retry_waits) == 2                                                         # 3번째 실패는 대기 없이 최종
    assert json.loads(_sent(p, "profiling.result")[0][1])["payload"]["error"]["code"] == "ANALYSIS_RETRY_EXHAUSTED"
    (row,) = d.rows.values()
    assert row.state == st.SETTLED and row.dlq_confirmed and row.analysis_attempts == 3


def test_재배달돼도_시도_예산은_초기화되지_않음(env):
    proc, d, _r, p, _c, a = env
    proc.handle(_delivery(_body())[0])
    (row,) = d.rows.values()
    d.rows[row.delivery_id] = replace(row, state=st.RUNNING, analysis_attempts=3, result_body=None, result_confirmed=False)
    assert proc.handle(_delivery(_body())[0]) is pr.Handled.ACKED and a.calls == 1
    assert json.loads(p.sent[-2][1])["payload"]["error"]["code"] == "ANALYSIS_RETRY_EXHAUSTED"


def test_재시도_대기_중_종료면_ACK_없이_STOPPED_다음_재배달은_대기_시각_뒤에(env):
    proc, d, _r, _p, c, a = env
    a.script = [(RunStatus.FAILED, ErrorCode.NO_ACTIVE_CATALOG)]
    c.stop_after_waits = 1
    dl, acks = _delivery(_body())
    assert proc.handle(dl) is pr.Handled.STOPPED and acks == []
    (row,) = d.rows.values()
    assert row.state == st.RETRY_WAIT and row.analysis_attempts == 1 and row.next_attempt_at > NOW


def test_추천_번호_규격_위반은_다시_해도_같으니_ANALYSIS_FAILED(env):
    proc, _d, _r, p, _c, a = env
    a.ids = [1, 1]
    assert proc.handle(_delivery(_body())[0]) is pr.Handled.ACKED and a.calls == 1
    assert json.loads(_sent(p, "profiling.result")[0][1])["payload"]["error"]["code"] == "ANALYSIS_FAILED"


def test_실패_결과는_확인됐고_실패큐만_남았으면_재배달_때_실패큐까지_마저(env):
    proc, d, _r, p, c, _a = env
    bad = json.dumps({"schemaVersion": 2, "requestId": RID, "createdAt": "2026-11-07T08:00:00Z",
                      "payload": {"recipientUserId": 42, "sourceVersion": 7}}).encode()
    p.script = [PublishResult.CONFIRMED, PublishResult.UNKNOWN]                            # 결과는 확인, 실패 큐는 불명
    c.stop_after_waits = 1
    dl, acks = _delivery(bad)
    assert proc.handle(dl) is pr.Handled.STOPPED and acks == []
    (row,) = d.rows.values()
    assert row.result_confirmed and row.dlq_required and not row.dlq_confirmed and row.state != st.SETTLED
    c.stopping, c.stop_after_waits = False, None
    assert proc.handle(_delivery(bad)[0]) is pr.Handled.ACKED
    assert len(_sent(p, "profiling.request.failed")) == 2                                  # 같은 원본을 다시(중복 허용 — 설계 §5)
    assert len({s[1] for s in _sent(p, "profiling.result")}) == 1                          # 실패 결과는 처음 바이트 그대로


def test_실패큐_확인이_끝난_SETTLED_재배달은_실패큐_복사_생략(env):
    proc, _d, _r, p, _c, _a = env
    proc.handle(_delivery(b"{not json")[0])
    proc.handle(_delivery(b"{not json")[0])
    assert len(_sent(p, "profiling.request.failed")) == 1


def test_잠금_커넥션이_끊기면_결과를_저장하지_않고_DB_장애로(env):
    proc, d, _r, _p, c, a = env
    d.lock_lost = 1
    dl, acks = _delivery(_body())
    assert proc.handle(dl) is pr.Handled.ACKED and acks == [1]
    assert ("pause", "db") in c.events and a.calls == 2                                     # 저장 못 한 실행은 다시(재분석 가능 — 설계 §4)


def test_연결이_바뀌면_대기를_그만두고_ABANDONED(env):
    proc, _d, r, _p, _c, a = env
    r.held.add((42, 7))
    alive = [True]
    dl, acks = _delivery(_body())
    dl._alive = lambda: alive[0]
    _c.on_wait = lambda: alive.__setitem__(0, False)
    assert proc.handle(dl) is pr.Handled.ABANDONED and acks == [] and a.calls == 0


def test_멈춤은_이_처리가_건_것만_푼다(env):
    proc, d, _r, _p, c, _a = env
    c.pause("publish")                                                                      # 다른 작업이 건 멈춤
    d.fail_next = 1
    proc.handle(_delivery(_body())[0])
    assert c.paused == ["publish"]                                                           # 우리 것(db)만 풀림


# ---------------------------------------------------------------- decide() 순수 함수
def _row(**kw):
    base = {"delivery_id": uuid.uuid4(), "request_body_sha256": "a" * 64, "request_id": RID, "recipient_user_id": 42, "source_version": 7,
            "input_hash": "h", "is_binding": False, "state": st.RECEIVED, "analysis_attempts": 0, "next_attempt_at": None,
            "result_body": None, "result_confirmed": False, "dlq_required": False, "dlq_confirmed": False, "failure_code": None}
    base.update(kw)
    return st.DeliveryRow(**base)


def test_decide_처음이면_바인딩_만들고_분석():
    own = _row()
    plan = pr.decide(own, [], max_attempts=3, now=NOW)
    assert plan.action is pr.Action.ANALYZE and plan.bind and plan.target == own.delivery_id


def test_decide_결과_있으면_USE_STORED():
    assert pr.decide(_row(result_body=b"x"), [], max_attempts=3, now=NOW).action is pr.Action.USE_STORED


def test_decide_다른_requestId_같은_키_같은_입력은_충돌_아님_바인딩_없이_따로():
    other = _row(request_id=RID2, is_binding=True)
    plan = pr.decide(_row(), [other], max_attempts=3, now=NOW)
    assert plan.action is pr.Action.ANALYZE and not plan.bind


def test_decide_같은_요청_바인딩이_결과_없으면_바인딩_행을_이어서():
    b = _row(is_binding=True, state=st.RUNNING, analysis_attempts=1)
    plan = pr.decide(_row(), [b], max_attempts=3, now=NOW)
    assert plan.action is pr.Action.RESUME and plan.target == b.delivery_id and not plan.bind


def test_decide_재시도_대기_시각_전이면_RETRY_LATER():
    own = _row(is_binding=True, state=st.RETRY_WAIT, analysis_attempts=1, next_attempt_at=NOW + timedelta(seconds=30))
    assert pr.decide(own, [own], max_attempts=3, now=NOW).action is pr.Action.RETRY_LATER


def test_reusable_ids_는_결과_상태와_입력이_맞을_때만():
    ok = ProfileOutcome(recipient_user_id=1, source_version=1, status=RunStatus.SUPERSEDED, input_hash="h",
                        search=SearchResult(product_ids=[2, 1], query_text="", catalog_version_id=CAT))
    assert pr.reusable_ids(ok, "h") == [2, 1]
    assert pr.reusable_ids(ok, "z") is None
    assert pr.reusable_ids(ok.model_copy(update={"status": RunStatus.FAILED}), "h") is None
    assert pr.reusable_ids(None, "h") is None


def test_retry_delay_는_지수_상한_지터():
    assert pr.retry_delay(1, 10, 60, rand=lambda: 0.0) == 5.0
    assert pr.retry_delay(1, 10, 60, rand=lambda: 1.0) == 10.0
    assert pr.retry_delay(3, 10, 60, rand=lambda: 1.0) == 40.0
    assert pr.retry_delay(4, 10, 60, rand=lambda: 1.0) == 60.0                  # 상한
    assert pr.retry_delay(10_000, 10, 60, rand=lambda: 1.0) == 60.0             # 큰 횟수도 넘치지 않음


def test_decide_예산_소진이면_EXHAUSTED():
    own = _row(is_binding=True, state=st.RUNNING, analysis_attempts=3)
    assert pr.decide(own, [own], max_attempts=3, now=NOW).action is pr.Action.EXHAUSTED


def test_decide_재시도_대기_시각이_지났으면_ANALYZE():
    own = _row(is_binding=True, state=st.RETRY_WAIT, analysis_attempts=1, next_attempt_at=NOW - timedelta(seconds=1))
    assert pr.decide(own, [own], max_attempts=3, now=NOW).action is pr.Action.ANALYZE


# ---------------------------------------------------------------- 통합 연결 지점(record_input · admit)
def _proc_with(env, **hooks):
    _proc, d, r, p, c, a = env
    clock = c.clock
    proc = pr.Processor(deliveries=d, runs=r, analyze=a, publisher=p, control=c, max_attempts=3, wait_s=0.01,
                        retry_base_s=10, retry_max_s=60, now=clock, rand=lambda: 0.5, **hooks)
    return proc, d, r, p, c, a


def test_입력_기록_훅은_분석_전에_요청과_입력해시로_부름(env):
    seen = []
    proc, _d, _r, _p, _c, a = _proc_with(env, record_input=lambda rq, h: seen.append((rq.recipient_user_id, rq.source_version, h, a.calls)))
    assert proc.handle(_delivery(_body())[0]) is pr.Handled.ACKED
    (uid, sv, h, calls_then) = seen[0]
    assert (uid, sv) == (42, 7) and len(h) == 64 and calls_then == 0              # 분석보다 먼저


def test_입력_기록이_충돌이면_REQUEST_CONFLICT_실패결과_실패큐_분석_안_함(env):
    def conflict(rq, h):
        raise pr.InputConflict("다른 입력")
    proc, d, _r, p, _c, a = _proc_with(env, record_input=conflict)
    dl, acks = _delivery(_body())
    assert proc.handle(dl) is pr.Handled.ACKED and acks == [1] and a.calls == 0
    res = json.loads(_sent(p, "profiling.result")[-1][1])
    assert res["payload"]["outcome"] == "FAILED" and res["payload"]["error"]["code"] == "REQUEST_CONFLICT"
    assert _sent(p, "profiling.request.failed")[0][4]["failure_code"] == "REQUEST_CONFLICT"
    (row,) = d.rows.values()
    assert row.state == st.SETTLED and row.analysis_attempts == 0


def test_입력_기록이_저장_불가면_INVALID_REQUEST_실패결과_실패큐(env):
    def invalid(rq, h):
        raise pr.InputInvalid("NUL")
    proc, _d, _r, p, _c, a = _proc_with(env, record_input=invalid)
    assert proc.handle(_delivery(_body())[0]) is pr.Handled.ACKED and a.calls == 0
    res = json.loads(_sent(p, "profiling.result")[-1][1])
    assert res["payload"]["outcome"] == "FAILED" and res["payload"]["error"]["code"] == "INVALID_REQUEST"
    assert _sent(p, "profiling.request.failed")[0][4]["failure_code"] == "INVALID_REQUEST"


def test_입력_기록의_다른_오류는_DB_장애로_보고_ACK_안_함(env):
    def broken(rq, h):
        raise RuntimeError("db")
    proc, _d, _r, _p, c, a = _proc_with(env, record_input=broken)
    c.stop_after_waits = 1
    dl, acks = _delivery(_body())
    assert proc.handle(dl) is pr.Handled.STOPPED and acks == [] and a.calls == 0 and ("pause", "db") in c.events


def test_분석_관문이_닫히면_카탈로그_일시_오류로_예산을_쓰고_재시도_열리면_분석(env):
    """설계 §5 처리 실패 1 · 재시도 소유권: 카탈로그 일시 오류는 예산 안 지연 재시도(11-08 설계대로 — 예산 안 쓰던 해석 A3 철회)."""
    from profiling.ports import NoActiveCatalog
    state = {"closed": 2, "entered": 0, "exited": 0}

    @contextmanager
    def admit():
        if state["closed"]:
            state["closed"] -= 1
            raise NoActiveCatalog("교체 중")
        state["entered"] += 1
        try:
            yield
        finally:
            state["exited"] += 1
    proc, d, _r, _p, c, a = _proc_with(env, admit=admit)
    dl, acks = _delivery(_body())
    assert proc.handle(dl) is pr.Handled.ACKED and acks == [1] and a.calls == 1
    assert state == {"closed": 0, "entered": 1, "exited": 1}                       # 관문은 분석 동안만 쥔다
    (row,) = d.rows.values()
    snap = proc.stats.snapshot()
    assert row.analysis_attempts == 3 and snap["admit_closed"] == 2 and snap["analysis.retry_wait"] == 2
    assert c.paused == []                                                          # 소비 전체를 멈추지 않는다


def test_분석_관문이_계속_닫히면_예산_소진_ANALYSIS_RETRY_EXHAUSTED(env):
    from profiling.ports import NoActiveCatalog

    def admit():
        raise NoActiveCatalog("교체 중")
    proc, d, _r, p, _c, a = _proc_with(env, admit=admit)
    dl, acks = _delivery(_body())
    assert proc.handle(dl) is pr.Handled.ACKED and acks == [1] and a.calls == 0
    res = json.loads(_sent(p, "profiling.result")[-1][1])
    assert res["payload"]["outcome"] == "FAILED" and res["payload"]["error"]["code"] == "ANALYSIS_RETRY_EXHAUSTED"
    assert _sent(p, "profiling.request.failed")[0][4]["failure_code"] == "ANALYSIS_RETRY_EXHAUSTED"
    assert next(iter(d.rows.values())).analysis_attempts == 3


def test_저장된_결과_재사용도_관문_안에서(env):
    _proc, _d, r, _p, _c, a = env
    from profiling.mq.messages import parse_request
    ih = parse_request(_body(), RID).input_hash
    r.runs[(42, 7)] = ProfileOutcome(recipient_user_id=42, source_version=7, status=RunStatus.RESULT_READY, input_hash=ih,
                                     search=SearchResult(product_ids=[8, 1], query_text="", catalog_version_id=CAT))
    entered = []

    @contextmanager
    def admit():
        entered.append(1)
        yield
    proc, *_ = _proc_with(env, admit=admit)
    assert proc.handle(_delivery(_body())[0]) is pr.Handled.ACKED and a.calls == 0 and entered == [1]
