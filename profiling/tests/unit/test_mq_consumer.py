"""mq.consumer · mq.config — 브로커 없이 가짜 연결로 시험 (설계 §5 · §7 · §8 체크리스트 2)."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pika.exceptions
import pytest

from profiling.mq import config as mqc
from profiling.mq.consumer import RequestConsumer
from profiling.settings import Settings


# ---------------------------------------------------------------------------
# 가짜 브로커 — 큐 하나, unacked 는 연결이 닫히면 큐 앞으로 되돌아간다(재배달 표시)
# ---------------------------------------------------------------------------
class FakeBroker:
    def __init__(self, messages, queue_exists=True):
        self.lock = threading.Lock()
        self.ready = [(b, mid, False) for b, mid in messages]
        self.acked: list[bytes] = []
        self.queue_exists = queue_exists
        self.drop_next = False        # 다음 이벤트 처리에서 연결이 끊긴 것처럼
        self.cancel_next = False      # 다음 이벤트 처리에서 브로커가 소비자만 취소(consumer timeout 등) — 채널은 열린 채
        self.max_unacked = 0
        self.connections = 0

    def connect(self):
        self.connections += 1
        return FakeConnection(self)


class FakeChannel:
    def __init__(self, conn):
        self.conn, self.is_open = conn, True
        self.prefetch, self.consumer = None, None
        self.unacked: dict[int, tuple[bytes, str]] = {}
        self.next_tag = 1
        self.cancel_cbs: list = []

    def add_on_cancel_callback(self, cb):
        self.cancel_cbs.append(cb)

    def basic_qos(self, prefetch_count):
        self.prefetch = prefetch_count

    def queue_declare(self, queue, passive):
        assert passive is True                           # AI 는 선언하지 않는다
        if not self.conn.broker.queue_exists:
            raise pika.exceptions.ChannelClosedByBroker(404, "NOT_FOUND")

    def basic_consume(self, queue, on_message_callback, auto_ack):
        assert auto_ack is False                         # 수동 ACK
        self.consumer = on_message_callback
        return "ctag"

    def basic_cancel(self, tag):
        self.consumer = None

    def basic_ack(self, delivery_tag):
        b, _ = self.unacked.pop(delivery_tag)
        with self.conn.broker.lock:
            self.conn.broker.acked.append(b)


class FakeConnection:
    def __init__(self, broker):
        self.broker, self.is_open = broker, True
        self.ch = None
        self.cbs: list = []
        self.cb_lock = threading.Lock()

    def channel(self):
        self.ch = FakeChannel(self)
        return self.ch

    def add_callback_threadsafe(self, cb):
        if not self.is_open:
            raise pika.exceptions.ConnectionWrongStateError()
        with self.cb_lock:
            self.cbs.append(cb)

    def process_data_events(self, time_limit):
        b = self.broker
        if b.drop_next:
            b.drop_next = False
            self.close()
            raise pika.exceptions.StreamLostError("lost")
        if b.cancel_next and self.ch.consumer:                # RabbitMQ 4.4 quorum 실측: 소비자 취소 + 미ACK 를 ready 로(채널은 열림)
            b.cancel_next = False
            self.ch.consumer = None
            with b.lock:
                b.ready[:0] = [(bd, mid, True) for bd, mid in self.ch.unacked.values()]
            self.ch.unacked.clear()                           # 돌려놓은 메시지의 tag 는 이제 무효
            for cb in self.ch.cancel_cbs:
                cb(SimpleNamespace(method=SimpleNamespace(consumer_tag="ctag")))
        with self.cb_lock:
            cbs, self.cbs = self.cbs, []
        for cb in cbs:
            cb()
        ch = self.ch
        while ch.consumer and len(ch.unacked) < ch.prefetch:
            with b.lock:
                if not b.ready:
                    break
                body, mid, red = b.ready.pop(0)
            tag = ch.next_tag
            ch.next_tag += 1
            ch.unacked[tag] = (body, mid)
            b.max_unacked = max(b.max_unacked, len(ch.unacked))
            ch.consumer(ch, SimpleNamespace(delivery_tag=tag, redelivered=red), SimpleNamespace(message_id=mid), body)
        time.sleep(0.005)

    def close(self):
        if not self.is_open:
            return
        self.is_open = False
        if self.ch:
            self.ch.is_open = False
            with self.broker.lock:                       # 미ACK 는 재배달
                self.broker.ready[:0] = [(bd, mid, True) for bd, mid in self.ch.unacked.values()]
            self.ch.unacked.clear()


def _cfg(**kw):
    base = {"url": "amqp://x", "prefetch": 4, "slots": 2, "reconnect_delay_s": 0.05, "shutdown_grace_s": 2.0,
            "confirm_timeout_s": 1.0, "wait_s": 0.05, "max_analysis_attempts": 3,
            "retry_base_s": 0.05, "retry_max_s": 0.2, "reconcile_after_s": 60, "reconcile_interval_s": 30}
    base.update(kw)
    return mqc.MqConfig(**base)


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _msgs(n):
    return [(f"m{i}".encode(), f"id{i}") for i in range(n)]


# ---------------------------------------------------------------------------
def test_받아서_처리하고_ACK():
    broker = FakeBroker(_msgs(5))
    seen = []
    c = RequestConsumer(_cfg(), lambda d: (seen.append(d.body), d.ack()), connect=broker.connect)
    c.start()
    assert _wait(lambda: len(broker.acked) == 5)
    c.stop()
    assert sorted(seen) == sorted(broker.acked)


def test_동시_분석은_슬롯_수_이하_받아_두는_수는_prefetch_이하():
    broker = FakeBroker(_msgs(12))
    lock, now, peak = threading.Lock(), [0], [0]

    def handler(d):
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        time.sleep(0.03)
        with lock:
            now[0] -= 1
        d.ack()

    c = RequestConsumer(_cfg(prefetch=3, slots=2), handler, connect=broker.connect)
    c.start()
    assert _wait(lambda: len(broker.acked) == 12)
    c.stop()
    assert peak[0] <= 2
    assert broker.max_unacked <= 3


def test_준비되기_전에는_소비하지_않음():
    broker = FakeBroker(_msgs(2))
    ready = threading.Event()
    c = RequestConsumer(_cfg(), lambda d: d.ack(), ready=ready.is_set, connect=broker.connect)
    c.start()
    assert _wait(lambda: c.status()["connected"])
    time.sleep(0.3)
    assert broker.acked == [] and not c.status()["consuming"]
    ready.set()
    assert _wait(lambda: len(broker.acked) == 2)
    c.stop()


def test_멈춤_중에는_새로_받지_않고_재개하면_이어서():
    broker = FakeBroker(_msgs(3))
    c = RequestConsumer(_cfg(prefetch=1, slots=1), lambda d: d.ack(), connect=broker.connect)
    c.pause("결과 발행 불가")
    c.start()
    assert _wait(lambda: c.status()["connected"])
    time.sleep(0.3)
    assert broker.acked == [] and c.status()["paused"] == ["결과 발행 불가"]
    c.resume("결과 발행 불가")
    assert _wait(lambda: len(broker.acked) == 3)
    c.stop()


def test_멈춤은_이유별로_세고_모두_풀려야_재개():
    c = RequestConsumer(_cfg(), lambda d: None, connect=lambda: None)
    c.pause("db")                     # 작업 A
    c.pause("db")                     # 작업 B
    c.pause("publish")                # 작업 C
    c.resume("db")                    # A 만 풀림 — B · C 의 멈춤은 남는다
    assert c.paused == ["db", "publish"]
    c.resume("publish")
    assert c.paused == ["db"]
    c.resume("db")
    assert c.paused == []
    c.resume("db")                    # 이미 풀린 것을 또 풀어도 음수가 되지 않는다
    assert c.paused == []


def test_연결이_끊기면_옛_tag_는_버리고_재배달을_다시_처리():
    broker = FakeBroker(_msgs(1))
    gate = threading.Event()
    calls = []

    def handler(d):
        calls.append(d.redelivered)
        if len(calls) == 1:
            broker.drop_next = True                       # 처리 중에 연결이 끊김
            assert _wait(lambda: broker.connections >= 2)
            assert d.stale                                # 연결이 바뀜 — 기다리던 작업은 그만둘 수 있다
            assert d.ack() is False                       # 옛 연결의 tag — ACK 하지 않는다
            gate.set()
            return
        assert not d.stale
        d.ack()

    c = RequestConsumer(_cfg(), handler, connect=broker.connect)
    c.start()
    assert _wait(lambda: len(broker.acked) == 1)
    c.stop()
    assert gate.is_set() and calls == [False, True]


def test_브로커가_소비자만_취소해도_다시_연결해_재배달을_처리():
    """consumer timeout 초과 · 큐 삭제 — 채널이 열린 채 소비자만 취소되면, 그냥 두면 '소비 중' 으로 보이며 영영 받지 않는다."""
    broker = FakeBroker(_msgs(1))
    calls = []

    def handler(d):
        calls.append(d.redelivered)
        if len(calls) == 1:
            broker.cancel_next = True
            assert _wait(lambda: d.stale)                     # 다시 연결 → 옛 tag 는 못 씀
            assert d.ack() is False
            return
        d.ack()

    c = RequestConsumer(_cfg(), handler, connect=broker.connect)
    c.start()
    assert _wait(lambda: len(broker.acked) == 1)
    c.stop()
    assert calls == [False, True] and broker.connections >= 2


def test_큐가_없으면_소비하지_않고_다시_연결을_시도():
    broker = FakeBroker(_msgs(1), queue_exists=False)
    c = RequestConsumer(_cfg(), lambda d: d.ack(), connect=broker.connect)
    c.start()
    assert _wait(lambda: broker.connections >= 3)
    assert broker.acked == []
    broker.queue_exists = True
    assert _wait(lambda: len(broker.acked) == 1)
    c.stop()


def test_종료는_진행_중_작업을_기다려_ACK():
    broker = FakeBroker(_msgs(1))
    started = threading.Event()

    def handler(d):
        started.set()
        time.sleep(0.4)
        d.ack()

    c = RequestConsumer(_cfg(shutdown_grace_s=3.0), handler, connect=broker.connect)
    c.start()
    assert started.wait(3)
    c.stop()
    assert broker.acked == [b"m0"]


def test_유예를_넘기면_ACK_없이_닫아_재배달되게():
    broker = FakeBroker(_msgs(1))
    started, release = threading.Event(), threading.Event()

    def handler(d):
        started.set()
        release.wait(5)
        d.ack()

    c = RequestConsumer(_cfg(shutdown_grace_s=0.2), handler, connect=broker.connect)
    c.start()
    assert started.wait(3)
    c.stop()
    release.set()
    assert broker.acked == []
    assert broker.ready == [(b"m0", "id0", True)]


def test_처리_예외는_ACK_하지_않음():
    broker = FakeBroker(_msgs(1))
    done = threading.Event()

    def handler(d):
        done.set()
        raise RuntimeError("boom")

    c = RequestConsumer(_cfg(), handler, connect=broker.connect)
    c.start()
    assert done.wait(3)
    assert _wait(lambda: c.status()["inflight"] == 0)
    c.stop()
    assert broker.acked == []


# ---------------------------------------------------------------------------
def _settings(**kw):
    base = {"MQ_ENABLED": True, "MQ_URL": "amqp://localhost:5672/gift", "MQ_PREFETCH": 8, "MQ_SLOTS": 8,
            "MQ_RECONNECT_DELAY_S": 5, "MQ_SHUTDOWN_GRACE_S": 30, "MQ_CONFIRM_TIMEOUT_S": 10, "MQ_WAIT_S": 2,
            "MQ_MAX_ANALYSIS_ATTEMPTS": 3, "MQ_RETRY_BASE_S": 5, "MQ_RETRY_MAX_S": 60,
            "MQ_RECONCILE_AFTER_S": 3600, "MQ_RECONCILE_INTERVAL_S": 300}
    base.update(kw)
    return Settings(_env_file=None, **base)


def test_설정_정상():
    cfg = mqc.load(_settings())
    assert (cfg.prefetch, cfg.slots) == (8, 8)


@pytest.mark.parametrize("kw, word", [
    ({"MQ_URL": ""}, "MQ_URL"), ({"MQ_URL": "http://x"}, "MQ_URL"),
    ({"MQ_PREFETCH": None}, "MQ_PREFETCH: 없음"), ({"MQ_SLOTS": 0}, "MQ_SLOTS: 0 이하"),
    ({"MQ_RECONNECT_DELAY_S": -1}, "MQ_RECONNECT_DELAY_S"), ({"MQ_SHUTDOWN_GRACE_S": None}, "MQ_SHUTDOWN_GRACE_S"),
    ({"MQ_PREFETCH": 4, "MQ_SLOTS": 8}, "PREFETCH < "),
    ({"MQ_CONFIRM_TIMEOUT_S": None}, "MQ_CONFIRM_TIMEOUT_S: 없음"), ({"MQ_WAIT_S": 0}, "MQ_WAIT_S: 0 이하"),
    ({"MQ_MAX_ANALYSIS_ATTEMPTS": None}, "MQ_MAX_ANALYSIS_ATTEMPTS: 없음"),
    ({"MQ_RETRY_BASE_S": None}, "MQ_RETRY_BASE_S: 없음"), ({"MQ_RETRY_MAX_S": 0}, "MQ_RETRY_MAX_S: 0 이하"),
    ({"MQ_RETRY_BASE_S": 30, "MQ_RETRY_MAX_S": 10}, "RETRY_MAX_S < "),
    ({"MQ_RECONCILE_AFTER_S": None}, "MQ_RECONCILE_AFTER_S: 없음"), ({"MQ_RECONCILE_INTERVAL_S": 0}, "MQ_RECONCILE_INTERVAL_S: 0 이하"),
    ({"MQ_RECONCILE_AFTER_S": 60, "MQ_RETRY_MAX_S": 60}, "RECONCILE_AFTER_S <= "),
])
def test_설정이_빠졌거나_0_음수_모순이면_거부(kw, word):
    with pytest.raises(mqc.MqConfigError) as e:
        mqc.load(_settings(**kw))
    assert any(word in p for p in e.value.problems)


def test_MQ_URL_은_설정_출력과_repr_에_나오지_않음():
    user, pw = "acct", "pw-not-shown"                                   # 가짜 값 — 주소에 바로 쓰지 않아 노출 점검에 안 걸리게
    s = _settings(MQ_URL=f"amqp://{user}:{pw}@localhost:5672/gift")
    assert "pw-not-shown" not in repr(s) and "pw-not-shown" not in str(s.model_dump())
    cfg = mqc.load(s)
    assert "pw-not-shown" not in repr(cfg) and cfg.url.endswith("/gift")
