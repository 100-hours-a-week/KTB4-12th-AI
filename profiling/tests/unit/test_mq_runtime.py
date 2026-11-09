"""체크리스트 8 — 서버 연결(조립 · DB 풀 · /health) · 가짜 브로커로 끝까지 · 통합 런타임 연결 지점.

임시 목: 가짜 브로커(test_mq_consumer.FakeBroker) · 메모리 전달 기록(test_mq_process.MemDeliveries) · 가짜 발행기 · 가짜 분석.
브로커 · DB · LLM 을 부르지 않는다(비용 0).
"""
import json
import time
from types import SimpleNamespace

import pytest

from profiling import main
from profiling.mq import runtime as rt
from profiling.mq.publisher import PublishResult
from profiling.settings import Settings
from tests.unit.test_mq_consumer import FakeBroker, _cfg, _wait
from tests.unit.test_mq_process import (
    RID,
    Analyzer,
    FakePublisher,
    MemDeliveries,
    MemRuns,
)
from tests.unit.test_mq_process import _body as mq_body


# ---------------------------------------------------------------- 8. DB 풀 · 준비 확인 캐시
def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_DB_풀은_MQ_슬롯마다_잠금_커넥션_2개_와_대조복구_1개를_더함():
    assert main.db_pool_size(_s(IO_THREADS=4)) == 4
    assert main.db_pool_size(_s(IO_THREADS=4, MQ_ENABLED=True, MQ_SLOTS=8)) == 4 + 16 + 1    # + 대조 복구 잠금 1
    assert main.db_pool_size(_s(IO_THREADS=4, MQ_ENABLED=False, MQ_SLOTS=8)) == 4            # 꺼져 있으면 더하지 않음


def test_DB_풀이_MQ_잠금까지_못_담으면_기동_거부():
    user, pw = "x", "y"                                                  # 가짜 값(연결하지 않음) — 주소에 바로 쓰지 않아 노출 점검에 안 걸리게
    s = _s(IO_THREADS=4, MQ_ENABLED=True, MQ_SLOTS=8, DB_POOL_SIZE=10, DB_MAX_OVERFLOW=0,
           DATABASE_URL=f"postgresql+psycopg://{user}:{pw}@127.0.0.1:1/none")
    with pytest.raises(RuntimeError, match="MQ 잠금 17"):
        main._connect_db(s)


def test_준비_확인은_간격_동안_다시_묻지_않음():
    calls, t = [], [0.0]
    c = rt._Cached(lambda: calls.append(1) or True, 5.0, clock=lambda: t[0])
    assert c() and c() and len(calls) == 1
    t[0] = 5.0
    assert c() and len(calls) == 2


# ---------------------------------------------------------------- 8. /health 의 mq 칸
def test_health_mq_칸_꺼짐():
    from tests.unit.test_health_contract import _Catalog, _state
    body = main._health_sync(_state(_Catalog(1)))
    assert body["mq"] == {"enabled": False} and body["store"]["connected"] in (True, False)


def test_health_mq_칸_켜짐_DB_오류여도_200_모양():
    from tests.unit.test_health_contract import _Catalog, _state

    class Broken(MemDeliveries):
        def health_counts(self):
            raise ConnectionError("db")

    r = rt.MqRuntime(_cfg(), deliveries=Broken(), runs=MemRuns(), analyze=Analyzer(), ready=lambda: True,
                     publisher=FakePublisher(), connect=lambda: None)
    st = _state(_Catalog(1))
    st.mq = r
    mq = main._health_sync(st)["mq"]
    assert mq["enabled"] and mq["deliveries"] == {"error": "ConnectionError"}
    assert set(mq["consumer"]) >= {"connected", "consuming", "paused", "inflight", "slots", "prefetch"}


# ---------------------------------------------------------------- 8. 조립 · 끝까지(가짜 브로커 → 소비자 → 처리 → 가짜 발행)
class Deliveries(MemDeliveries):
    def health_counts(self):
        rows = [r for r in self.rows.values() if r.state != "SETTLED"]
        return {"open": len(rows), "dlq_pending": sum(r.dlq_required and not r.dlq_confirmed for r in rows)}


def _runtime(broker, publisher=None, analyze=None):
    d = Deliveries()
    pub = publisher or FakePublisher()
    r = rt.MqRuntime(_cfg(slots=2, prefetch=4, wait_s=0.01), deliveries=d, runs=MemRuns(), analyze=analyze or Analyzer(),
                     ready=lambda: True, publisher=pub, connect=broker.connect)
    pub.start = pub.stop = lambda: None
    return r, d, pub


def test_가짜_브로커로_끝까지_정상_요청_결과_발행_ACK():
    broker = FakeBroker([(mq_body(), RID)])
    r, d, pub = _runtime(broker)
    r.start()
    try:
        assert _wait(lambda: len(broker.acked) == 1)
    finally:
        r.stop()
    (row,) = d.rows.values()
    assert row.state == "SETTLED" and json.loads(pub.sent[0][1])["payload"]["outcome"] == "SUCCEEDED"
    assert r.stats.snapshot()["handled.acked"] == 1 and r.health()["deliveries"] == {"open": 0, "dlq_pending": 0}


def test_가짜_브로커로_끝까지_손상_메시지는_실패큐로_ACK():
    broker = FakeBroker([(b"{oops", None)])
    r, _d, pub = _runtime(broker)
    r.start()
    try:
        assert _wait(lambda: len(broker.acked) == 1)
    finally:
        r.stop()
    assert [s[0] for s in pub.sent] == ["profiling.request.failed"]


def test_발행이_계속_안_되면_소비를_멈추고_ACK_안_함_종료_때_재배달():
    broker = FakeBroker([(mq_body(), RID), (mq_body(rid="0b5c2b1e-7e0a-4b0e-9a55-2f2f6a3f9c11", uid=43), None)])
    pub = FakePublisher([PublishResult.UNKNOWN] * 1000)
    r, _d, _ = _runtime(broker, publisher=pub)
    r.start()
    try:
        assert _wait(lambda: r.consumer.paused == ["publish"])
        time.sleep(0.2)
        assert broker.acked == []
    finally:
        r.stop()
    assert broker.acked == []


def test_build_는_설정이_모자라면_거부():
    from profiling.mq.config import MqConfigError
    with pytest.raises(MqConfigError):
        rt.build(SimpleNamespace(engine=None), _s(MQ_ENABLED=True), ready=lambda: True)


def test_대조_복구_스레드가_간격마다_돌아_빠진_발행을_마저():
    from dataclasses import replace

    from profiling.mq import store as st

    class Stale(Deliveries):
        def stale_open(self, older_than_s, limit):
            return [r for r in self.rows.values() if r.state != "SETTLED"]

    d, pub = Stale(), FakePublisher()
    pub.start = pub.stop = lambda: None
    r = d.receive("ab" * 32)
    d.rows[r.delivery_id] = replace(r, request_id=RID, result_body=b"R", state=st.RESULT_READY)
    x = rt.MqRuntime(_cfg(reconcile_interval_s=0.05), deliveries=d, runs=MemRuns(), analyze=Analyzer(), ready=lambda: False,
                     publisher=pub, connect=FakeBroker([]).connect)
    x.start()
    try:
        assert _wait(lambda: d.rows[r.delivery_id].state == "SETTLED")
    finally:
        x.stop()
    assert pub.sent[0][1] == b"R" and x.stats.snapshot()["reconcile.republished"] == 1


# ---------------------------------------------------------------- 통합 런타임 연결(시작 미루기 · 늦게 읽기)
def test_시작_전_stop_은_아무것도_안_함():
    pub = FakePublisher()
    pub.start = lambda: (_ for _ in ()).throw(AssertionError("시작 안 했는데 발행기를 건드림"))
    pub.stop = pub.start
    r = rt.MqRuntime(_cfg(), deliveries=Deliveries(), runs=MemRuns(), analyze=Analyzer(), ready=lambda: True,
                     publisher=pub, connect=FakeBroker([]).connect)
    r.stop()
    assert not r.started


def test_build_는_저장소_입력기록_관문을_app_state_에서_부를_때마다_읽음(monkeypatch):
    from contextlib import contextmanager

    monkeypatch.setattr(rt, "DbMqDeliveryStore", lambda engine: Deliveries())
    monkeypatch.setattr(rt.mqc, "load", lambda settings: _cfg())               # 설정 검사는 test_build_는_설정이_모자라면_거부 가 맡음
    state = SimpleNamespace(engine=None, store="처음 것")
    proc = rt.build(state, _s(), ready=lambda: True).processor
    state.store = SimpleNamespace(get_run=lambda *a: "감싼 것")              # 통합 런타임이 나중에 감쌈
    assert proc.runs.get_run(1, 2) == "감싼 것"

    seen, entered = [], []
    proc.record_input("rq", "h")                                             # 아직 훅 없음 → 조용히 지나감
    state.mq_record_input = lambda rq, h: seen.append((rq, h))
    proc.record_input("rq", "h")
    assert seen == [("rq", "h")]

    with proc.admit():
        pass
    @contextmanager
    def gate():
        entered.append(1)
        yield
    state.mq_admit = gate
    with proc.admit():
        pass
    assert entered == [1]
