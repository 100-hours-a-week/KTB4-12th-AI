"""mq.publisher — 확인(Confirm) · Return · Nack · 불명 판정, 발행 속성, 끊김 뒤 다시 연결 (가짜 연결 · 브로커 없음 · 비용 0)."""
import threading
import time

import pika.exceptions
import pytest

from profiling.mq import publisher as pub

RID = "8a3851f6-1a29-4f44-a812-f6d05ad3d8e1"


class FakeChannel:
    def __init__(self, conn):
        self.conn, self.confirm, self.sent = conn, False, []

    def confirm_delivery(self):
        self.confirm = True

    def basic_publish(self, *, exchange, routing_key, body, properties, mandatory):
        assert self.confirm, "confirm_delivery 전에 발행하면 안 된다"
        mode = self.conn.script.pop(0) if self.conn.script else "ok"
        if mode == "hang":
            time.sleep(0.5)
            mode = "ok"
        self.sent.append({"exchange": exchange, "routing_key": routing_key, "body": body, "props": properties, "mandatory": mandatory})
        if mode == "return":
            raise pika.exceptions.UnroutableError([])
        if mode == "nack":
            raise pika.exceptions.NackError([])
        if mode == "drop":
            raise pika.exceptions.StreamLostError("lost")


class FakeConn:
    def __init__(self, script):
        self.script, self.is_open = script, True
        self.ch = FakeChannel(self)

    def channel(self):
        return self.ch

    def process_data_events(self, time_limit=0):
        pass

    def close(self):
        self.is_open = False


@pytest.fixture
def make():
    made = []

    def _make(script=(), timeout=1.0):
        conns = []
        shared = list(script)

        def connect():
            c = FakeConn(shared)
            conns.append(c)
            return c
        p = pub.Publisher(connect, confirm_timeout_s=timeout, reconnect_delay_s=0.01)
        p.start()
        made.append(p)
        return p, conns
    yield _make
    for p in made:
        p.stop()


def test_확인되면_CONFIRMED_와_발행_속성(make):
    p, conns = make()
    assert p.publish(pub.RESULT_ROUTING_KEY, b"{}", message_id=RID, correlation_id=RID) is pub.PublishResult.CONFIRMED
    s = conns[0].ch.sent[0]
    assert s["exchange"] == "profiling.exchange" and s["routing_key"] == "profiling.result" and s["mandatory"] is True
    pr = s["props"]
    assert pr.delivery_mode == 2 and pr.content_type == "application/json" and pr.content_encoding == "UTF-8"
    assert pr.message_id == RID and pr.correlation_id == RID


def test_Return_은_실패(make):
    p, _ = make(["return"])
    assert p.publish(pub.RESULT_ROUTING_KEY, b"{}", message_id=RID) is pub.PublishResult.RETURNED


def test_Nack_은_실패(make):
    p, _ = make(["nack"])
    assert p.publish(pub.RESULT_ROUTING_KEY, b"{}", message_id=RID) is pub.PublishResult.NACKED


def test_연결이_끊기면_UNKNOWN_이고_다시_연결해_다음_발행은_됨(make):
    p, conns = make(["drop"])
    assert p.publish(pub.RESULT_ROUTING_KEY, b"a", message_id=RID) is pub.PublishResult.UNKNOWN
    assert p.publish(pub.RESULT_ROUTING_KEY, b"a", message_id=RID) is pub.PublishResult.CONFIRMED
    assert len(conns) == 2 and conns[0].is_open is False


def test_확인_시간_초과는_UNKNOWN(make):
    p, _ = make(["hang"], timeout=0.1)
    assert p.publish(pub.RESULT_ROUTING_KEY, b"a", message_id=RID) is pub.PublishResult.UNKNOWN


def test_멈춘_뒤엔_UNKNOWN(make):
    p, _ = make()
    p.stop()
    assert p.publish(pub.RESULT_ROUTING_KEY, b"a", message_id=RID) is pub.PublishResult.UNKNOWN


def test_여러_스레드가_동시에_발행해도_한_채널로_차례대로(make):
    p, conns = make()
    out = []
    ts = [threading.Thread(target=lambda i=i: out.append(p.publish(pub.RESULT_ROUTING_KEY, str(i).encode(), message_id=RID)))
          for i in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert out.count(pub.PublishResult.CONFIRMED) == 10 and len(conns) == 1 and len(conns[0].ch.sent) == 10
