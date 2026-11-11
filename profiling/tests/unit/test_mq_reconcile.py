"""mq.reconcile — 대조 복구(설계 CO-06 · CO-02): 장기 미완료 기록에서 빠진 단계만 마저 한다.

임시 목: 메모리 전달 기록(test_mq_process.MemDeliveries + stale_open) · 가짜 발행기. 브로커 · DB · LLM 없음(비용 0).
"""
import uuid
from dataclasses import replace

from profiling.mq import reconcile as rc
from profiling.mq import store as st
from profiling.mq.publisher import PublishResult
from tests.unit.test_mq_process import RID, FakePublisher, MemDeliveries


class Deliveries(MemDeliveries):
    def stale_open(self, older_than_s, limit):
        self.asked = (older_than_s, limit)
        return [r for r in self.rows.values() if r.state != st.SETTLED][:limit]


def _row(d, **kw):
    r = d.receive(uuid.uuid4().hex * 2)
    d.rows[r.delivery_id] = replace(r, **kw)
    return d.rows[r.delivery_id]


def _rc(d, p=None):
    return rc.Reconciler(d, p or FakePublisher(), older_than_s=3600, batch=50)


def test_결과는_있는데_발행_확인이_없으면_같은_바이트_재발행_후_SETTLED():
    d, p = Deliveries(), FakePublisher()
    r = _row(d, request_id=RID, result_body=b"R", state=st.RESULT_READY)
    rep = _rc(d, p).run_once()
    assert rep.counts == {"REPUBLISHED": 1} and rep.manual == []
    assert p.sent[0][:4] == ("profiling.result", b"R", RID, RID)
    g = d.rows[r.delivery_id]
    assert g.result_confirmed and g.state == st.SETTLED and d.asked == (3600, 50)


def test_재발행이_확인되지_않으면_기록은_그대로_다음_차례에():
    d = Deliveries()
    r = _row(d, request_id=RID, result_body=b"R", state=st.RESULT_READY)
    rep = _rc(d, FakePublisher([PublishResult.RETURNED])).run_once()
    assert rep.counts == {"PUBLISH_FAILED": 1}
    assert not d.rows[r.delivery_id].result_confirmed and d.rows[r.delivery_id].state == st.RESULT_READY


def test_확인은_다_됐는데_SETTLED_만_빠졌으면_SETTLED():
    d, p = Deliveries(), FakePublisher()
    r = _row(d, request_id=RID, result_body=b"R", result_confirmed=True, state=st.RESULT_READY)
    assert _rc(d, p).run_once().counts == {"SETTLED": 1}
    assert p.sent == [] and d.rows[r.delivery_id].state == st.SETTLED


def test_실패_큐_복사가_미확인이면_결과만_마저_보내고_사람이_대조():
    d, p = Deliveries(), FakePublisher()
    r = _row(d, request_id=RID, result_body=b"F", failure_code="ANALYSIS_FAILED", dlq_required=True, state=st.RESULT_READY)
    rep = _rc(d, p).run_once()
    assert rep.counts == {"DLQ_MANUAL": 1} and rep.manual == [(RID, "DLQ_MANUAL")]
    assert [s[0] for s in p.sent] == ["profiling.result"]                     # 실패 큐로는 보내지 않는다(원본 미보관)
    g = d.rows[r.delivery_id]
    assert g.result_confirmed and not g.dlq_confirmed and g.state != st.SETTLED


def test_식별_불가_실패큐_미확인은_원본_해시로_표시():
    d = Deliveries()
    r = _row(d, dlq_required=True, failure_code="UNIDENTIFIABLE")
    rep = _rc(d).run_once()
    assert rep.manual == [(f"sha:{r.request_body_sha256[:12]}", "DLQ_MANUAL")] and d.body_locks == [r.request_body_sha256]


def test_결과_없이_멈춘_것은_재분석하지_않고_대조_목록():
    d, p = Deliveries(), FakePublisher()
    _row(d, request_id=RID, state=st.RUNNING, analysis_attempts=1)
    rep = _rc(d, p).run_once()
    assert rep.counts == {"NO_RESULT": 1} and rep.manual == [(RID, "NO_RESULT")] and p.sent == []


def test_잠금을_다른_실행이_쥐면_건드리지_않음():
    d, p = Deliveries(), FakePublisher()
    r = _row(d, request_id=RID, result_body=b"R", state=st.RESULT_READY)
    d.held.add(RID)
    assert _rc(d, p).run_once().counts == {"BUSY": 1}
    assert p.sent == [] and not d.rows[r.delivery_id].result_confirmed


def test_잠근_뒤_다시_읽어_그사이_끝났으면_그대로():
    d, p = Deliveries(), FakePublisher()
    r = _row(d, request_id=RID, result_body=b"R", state=st.RESULT_READY)
    stale = d.stale_open
    d.stale_open = lambda s, n: [x for x in stale(s, n)]
    orig_get = d.get

    def get(i):                                                   # 잠근 직후 읽을 때는 이미 처리 함수가 끝낸 상태
        d.rows[i] = replace(d.rows[i], result_confirmed=True, state=st.SETTLED)
        return orig_get(i)
    d.get = get
    assert _rc(d, p).run_once().counts == {"ALREADY_SETTLED": 1} and p.sent == []
    assert d.rows[r.delivery_id].state == st.SETTLED


def test_요약은_마지막_결과를_남김_지표는_종류별_개수():
    d = Deliveries()
    _row(d, request_id=RID, result_body=b"R", state=st.RESULT_READY)
    x = _rc(d)
    x.run_once()
    assert x.last == {"counts": {"REPUBLISHED": 1}, "manual": 0}
    assert x.stats.snapshot()["reconcile.republished"] == 1


def test_읽기_전용_확인은_발행_기록하지_않음():
    d = Deliveries()
    _row(d, request_id=RID, result_body=b"R", state=st.RESULT_READY)
    _row(d, request_id=None, dlq_required=True)
    rows = rc.report_only(d, 3600, 10)
    assert sorted(k for _w, _s, k in rows) == ["DLQ_MANUAL", "REPUBLISHED"]
    assert all(not r.result_confirmed for r in d.rows.values() if r.result_body)
