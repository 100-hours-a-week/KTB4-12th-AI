"""실제 RabbitMQ(4.4) + 실제 PostgreSQL 로 MQ 소비 · 발행을 끝까지 (체크리스트 2 · 5 · 6 · 8 · CO-04 · CO-06).

켜는 법: PROFILING_TEST_AMQP_URL=amqp://<시험 계정>:<비밀번호>@127.0.0.1:5672/gift  (+ PROFILING_DATABASE_URL — 버리는 DB)
  없으면 이 파일 전체를 건너뛴다. 분석은 가짜(LLM 없음, 비용 0). 운영 · 개발 브로커에 붙이지 말 것 — 시험마다 세 큐를 비운다.
  그래서 호스트가 로컬(localhost · 127.x · ::1 · rabbitmq · host.docker.internal)이 아니면 시험을 시작하지 않는다(_require_local).

브로커 쪽 준비(BE/인프라 몫을 시험용으로 흉내 — AI 코드는 선언하지 않는다):
  vhost gift · direct profiling.exchange · quorum durable profiling.request / profiling.result / profiling.request.failed,
  각 큐 = 같은 이름 routing key 로 바인딩.
  ⚠️ profiling.request.failed 선언은 아직 BE 코드에 없다(BE-08 은 이름 · 선언 주체만 확정) — 여기서는 다른 두 큐와 같은 모양으로 가정.
관리 API(기본 http://<같은 호스트>:15672, PROFILING_TEST_AMQP_MGMT_URL 로 바꿈)는 연결 끊기 · unacked 수 확인 · 정책에만 쓴다.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import replace
from typing import ClassVar

import pika
import pytest
import sqlalchemy as sa

from profiling.mq import reconcile as rc
from profiling.mq import store as st
from profiling.mq.config import MqConfig
from profiling.mq.messages import parse_request
from profiling.mq.process import Handled
from profiling.mq.publisher import Publisher
from profiling.mq.runtime import MqRuntime
from profiling.settings import get_settings
from profiling.stores import DbProfileRunStore
from profiling.types import ErrorCode, ProfileOutcome, RunStatus, SearchResult

URL = os.environ.get("PROFILING_TEST_AMQP_URL", "")
LOCAL_HOSTS = frozenset({"localhost", "::1", "rabbitmq", "host.docker.internal"})


def _require_local(url: str) -> str:
    """시험이 세 큐를 비우므로(purge) 로컬 시험 브로커만 허용 — 공유 브로커(개발 · 운영) 주소면 거부."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in LOCAL_HOSTS or host.startswith("127.") or host.endswith(".localhost"):
        return url
    raise RuntimeError("PROFILING_TEST_AMQP_URL 은 로컬 시험 브로커만(localhost · 127.x · ::1 · rabbitmq · host.docker.internal) — 시험이 큐를 비운다")


if URL:
    _require_local(URL)
pytestmark = pytest.mark.skipif(not URL, reason="PROFILING_TEST_AMQP_URL 없음 — 실제 브로커 시험 건너뜀")

EX = "profiling.exchange"
REQ, RES, FAILED = "profiling.request", "profiling.result", "profiling.request.failed"
QUEUES = (REQ, RES, FAILED)
UID0 = 990_000_300


# ---------------------------------------------------------------- 브로커 조작(BE · 인프라 · 운영자 역할)
class Broker:
    def __init__(self, url: str) -> None:
        self.url = _require_local(url)
        u = urllib.parse.urlparse(url)
        self.vhost = urllib.parse.unquote(u.path.lstrip("/")) or "/"
        self.mgmt = os.environ.get("PROFILING_TEST_AMQP_MGMT_URL", f"http://{u.hostname}:15672").rstrip("/")
        auth = f"{urllib.parse.unquote(u.username or '')}:{urllib.parse.unquote(u.password or '')}"
        self._auth = "Basic " + base64.b64encode(auth.encode()).decode()

    def _ch(self):
        conn = pika.BlockingConnection(pika.URLParameters(self.url))
        return conn, conn.channel()

    def send(self, body: bytes, message_id: str | None) -> None:
        """BE 처럼 요청 발행: persistent · JSON/UTF-8 · message_id = requestId · mandatory · confirm."""
        conn, ch = self._ch()
        try:
            ch.confirm_delivery()
            ch.basic_publish(EX, REQ, body, pika.BasicProperties(content_type="application/json", content_encoding="UTF-8",
                                                                 delivery_mode=2, message_id=message_id), mandatory=True)
        finally:
            conn.close()

    def take(self, queue: str, n: int, timeout: float = 15.0) -> list[tuple[pika.BasicProperties, bytes]]:
        """큐에서 n 개를 꺼낸다(ACK). 시간 안에 못 모으면 모은 만큼."""
        out, end = [], time.monotonic() + timeout
        conn, ch = self._ch()
        try:
            while len(out) < n and time.monotonic() < end:
                m, props, body = ch.basic_get(queue, auto_ack=True)
                if m is None:
                    time.sleep(0.1)
                    continue
                out.append((props, body))
        finally:
            conn.close()
        return out

    def ready(self, queue: str) -> int:
        conn, ch = self._ch()
        try:
            return ch.queue_declare(queue, passive=True).method.message_count
        finally:
            conn.close()

    def purge(self) -> None:
        conn, ch = self._ch()
        try:
            for q in QUEUES:
                ch.queue_purge(q)
        finally:
            conn.close()

    def unbind_result(self) -> None:
        conn, ch = self._ch()
        try:
            ch.queue_unbind(RES, EX, routing_key=RES)
        finally:
            conn.close()

    def bind_result(self) -> None:
        conn, ch = self._ch()
        try:
            ch.queue_bind(RES, EX, routing_key=RES)
        finally:
            conn.close()

    # ---- 관리 API
    def _api(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(self.mgmt + "/api" + path, method=method, headers={"Authorization": self._auth,
                                                                                       "content-type": "application/json"},
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=5) as r:
            data = r.read()
        return json.loads(data) if data else None

    def unacked(self, queue: str) -> int:
        return int(self._api("GET", f"/queues/{urllib.parse.quote(self.vhost, safe='')}/{queue}").get("messages_unacknowledged", 0))

    def close_all_connections(self, at_least: int = 1) -> int:
        """관리 API 의 연결 목록은 통계 수집 주기만큼 늦게 찬다 — at_least 개가 보일 때까지 기다린 뒤 모두 끊는다."""
        conns: list = []
        end = time.monotonic() + 15
        while time.monotonic() < end:
            conns = self._api("GET", "/connections") or []
            if len(conns) >= at_least:
                break
            time.sleep(0.5)
        for c in conns:
            self._api("DELETE", "/connections/" + urllib.parse.quote(c["name"], safe=""))
        return len(conns)

    def set_policy(self, name: str, pattern: str, definition: dict, priority: int) -> None:
        self._api("PUT", f"/policies/{urllib.parse.quote(self.vhost, safe='')}/{name}",
                  {"pattern": pattern, "definition": definition, "priority": priority, "apply-to": "queues"})

    def delete_policy(self, name: str) -> None:
        self._api("DELETE", f"/policies/{urllib.parse.quote(self.vhost, safe='')}/{name}")


# ---------------------------------------------------------------- 가짜 분석(LLM 없음)
class FakeAnalyze:
    made: ClassVar[list[FakeAnalyze]] = []                        # 정리 때 멈춰 있는 분석을 모두 풀려고

    def __init__(self, ids=(11, 7, 3)) -> None:
        FakeAnalyze.made.append(self)
        self.ids = list(ids)
        self.script: list[tuple[RunStatus, ErrorCode | None]] = []
        self.gate: threading.Event | None = None                 # 있으면 열릴 때까지 분석이 멈춰 있다(오래 걸리는 분석 흉내)
        self.calls = 0
        self.now = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, rq):
        with self._lock:
            self.calls += 1
            self.now += 1
            self.peak = max(self.peak, self.now)
            status, code = self.script.pop(0) if self.script else (RunStatus.RESULT_READY, None)
        try:
            if self.gate is not None:
                self.gate.wait(60)
            return ProfileOutcome(recipient_user_id=rq.recipient_user_id, source_version=rq.source_version, status=status,
                                  input_hash="x", failure_code=code,
                                  search=SearchResult(product_ids=self.ids, query_text="", catalog_version_id=uuid.UUID(int=1))
                                  if status is RunStatus.RESULT_READY else None)
        finally:
            with self._lock:
                self.now -= 1


def _body(rid: str, uid: int, sv: int = 7, pref: str = "커피") -> bytes:
    return json.dumps({"schemaVersion": 1, "requestId": rid, "createdAt": "2026-11-08T08:00:00Z",
                       "payload": {"recipientUserId": uid, "sourceVersion": sv, "dislikedCategories": [], "giftPreference": pref,
                                   "reviews": []}}, ensure_ascii=False).encode()


class Runtime(MqRuntime):
    """처리 결과(Handled)를 모아 두는 MqRuntime — 그 밖은 서버와 같다(실제 Publisher · RequestConsumer · DB 저장소)."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.handled: list[Handled] = []

    def _handle(self, d) -> None:
        self.handled.append(self.processor.handle(d))


def _cfg(**over) -> MqConfig:
    base = MqConfig(url=URL, prefetch=4, slots=2, reconnect_delay_s=0.3, shutdown_grace_s=2, confirm_timeout_s=5, wait_s=0.2,
                    max_analysis_attempts=3, retry_base_s=0.2, retry_max_s=0.4, reconcile_after_s=3600, reconcile_interval_s=3600)
    return replace(base, **over)


def _wait(cond, timeout: float = 15.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


# ---------------------------------------------------------------- 고정 장치
@pytest.fixture(scope="module")
def broker():
    b = Broker(URL)
    try:
        b.ready(REQ)
        b._api("GET", "/overview")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"시험 브로커 · 관리 API 에 닿지 않음: {type(e).__name__}")
    return b


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
def env(broker, engine):
    """시험마다: 세 큐 비우기 · 이 시험이 쓴 요청의 DB 행 지우기 · 시작한 런타임 끄기."""
    made: dict[str, list] = {"rids": [], "uids": [], "shas": [], "runtimes": []}
    runs = DbProfileRunStore(engine)

    def wipe():
        with engine.begin() as c:
            c.execute(sa.text("delete from ai_profile.mq_deliveries where request_id::text = any(:r) or request_body_sha256 = any(:s)"),
                      {"r": made["rids"], "s": made["shas"]})
        for u in made["uids"]:
            runs.delete_recipient(u)

    def request(uid_off: int = 0, sv: int = 7, pref: str = "커피", rid: str | None = None) -> tuple[str, bytes]:
        rid = rid or str(uuid.uuid4())
        b = _body(rid, UID0 + uid_off, sv, pref)
        made["rids"].append(rid)
        made["uids"].append(UID0 + uid_off)
        made["shas"].append(parse_request(b, rid).body_sha256)
        return rid, b

    def raw(b: bytes) -> bytes:
        import hashlib
        made["shas"].append(hashlib.sha256(b).hexdigest())
        return b

    def runtime(analyze, **over) -> Runtime:
        r = Runtime(_cfg(**over), deliveries=st.DbMqDeliveryStore(engine), runs=runs, analyze=analyze, ready=lambda: True)
        made["runtimes"].append(r)
        r.start()
        assert _wait(lambda: r.consumer.status()["consuming"] and r.publisher.connected), "소비 · 발행 연결이 서지 않음"
        return r

    broker.purge()
    yield type("Env", (), {"request": staticmethod(request), "raw": staticmethod(raw), "runtime": staticmethod(runtime),
                           "deliveries": st.DbMqDeliveryStore(engine), "engine": engine})
    for r in made["runtimes"]:
        r.stop()
    for a in FakeAnalyze.made:                                    # 멈춘 분석을 풀어 작업 스레드를 끝낸다(행을 지우기 전에)
        if a.gate is not None:
            a.gate.set()
    for r in made["runtimes"]:
        r.consumer._pool.shutdown(wait=True)
    FakeAnalyze.made.clear()
    broker.bind_result()
    broker.purge()
    wipe()


def _row(env, rid):
    with env.engine.connect() as c:
        i = c.execute(sa.text("select delivery_id from ai_profile.mq_deliveries where request_id::text = :r and is_binding"),
                      {"r": rid}).scalar_one()
    return env.deliveries.get(i)


# ---------------------------------------------------------------- 시나리오
def test_정상_요청_결과_속성_ACK_DB_SETTLED(env, broker):
    a = FakeAnalyze()
    r = env.runtime(a)
    rid, body = env.request()
    broker.send(body, rid)
    (got,) = broker.take(RES, 1)
    props, res = got
    assert (props.message_id, props.correlation_id, props.delivery_mode, props.content_type, props.content_encoding) == \
        (rid, rid, 2, "application/json", "UTF-8")
    p = json.loads(res)
    assert p["requestId"] == rid and p["payload"]["outcome"] == "SUCCEEDED" and p["payload"]["data"]["recommendedProductIds"] == [11, 7, 3]
    assert _wait(lambda: r.handled == [Handled.ACKED])
    r.stop()                                                       # ACK 됐다면 연결을 닫아도 요청이 돌아오지 않는다
    assert broker.ready(REQ) == 0 and broker.ready(FAILED) == 0
    row = _row(env, rid)
    assert row.state == st.SETTLED and row.result_confirmed and a.calls == 1


def test_식별_불가는_결과_없이_원본_바이트를_실패큐로(env, broker):
    r = env.runtime(FakeAnalyze())
    bad = env.raw(b'{"schemaVersion":1,"requestId":"oops"')
    broker.send(bad, "not-a-uuid")
    (got,) = broker.take(FAILED, 1)
    props, b = got
    assert b == bad and props.message_id == "not-a-uuid" and props.delivery_mode == 2
    assert props.headers["failure_code"] == "UNIDENTIFIABLE" and props.headers["original_message_id"] == "not-a-uuid"
    assert len(props.headers["request_body_sha256"]) == 64
    assert _wait(lambda: r.handled == [Handled.ACKED]) and broker.ready(RES) == 0


def test_같은_수신자_버전에_다른_입력이면_충돌_실패결과와_실패큐(env, broker):
    r = env.runtime(FakeAnalyze(), slots=1, prefetch=1)              # 순서를 고정하려고 한 건씩
    rid1, b1 = env.request(uid_off=1, pref="커피")
    rid2, b2 = env.request(uid_off=1, pref="와인")
    broker.send(b1, rid1)
    assert _wait(lambda: len(r.handled) == 1)
    broker.send(b2, rid2)
    res = broker.take(RES, 2)
    by = {json.loads(b)["requestId"]: json.loads(b)["payload"] for _p, b in res}
    assert by[rid1]["outcome"] == "SUCCEEDED" and by[rid2]["outcome"] == "FAILED" and by[rid2]["error"]["code"] == "REQUEST_CONFLICT"
    (dl,) = broker.take(FAILED, 1)
    assert dl[1] == b2 and dl[0].headers["failure_code"] == "REQUEST_CONFLICT"
    assert _wait(lambda: r.handled == [Handled.ACKED, Handled.ACKED])


def test_분석_일시_실패는_메시지를_쥔_채_재시도_후_성공(env, broker):
    a = FakeAnalyze()
    a.script = [(RunStatus.FAILED, ErrorCode.PIPELINE_ERROR)]
    r = env.runtime(a)
    rid, body = env.request(uid_off=2)
    broker.send(body, rid)
    (got,) = broker.take(RES, 1)
    assert json.loads(got[1])["payload"]["outcome"] == "SUCCEEDED" and a.calls == 2
    assert _wait(lambda: r.handled == [Handled.ACKED]) and _row(env, rid).analysis_attempts == 2


def test_분석_예산_소진이면_RETRY_EXHAUSTED_실패결과(env, broker):
    a = FakeAnalyze()
    a.script = [(RunStatus.FAILED, ErrorCode.PIPELINE_ERROR)] * 3
    r = env.runtime(a)
    rid, body = env.request(uid_off=3)
    broker.send(body, rid)
    (got,) = broker.take(RES, 1)
    assert json.loads(got[1])["payload"]["error"]["code"] == "ANALYSIS_RETRY_EXHAUSTED" and a.calls == 3
    assert _wait(lambda: r.handled == [Handled.ACKED])


def test_결과큐_바인딩이_없으면_Return_소비멈춤_ACK안함_복구되면_마저(env, broker):
    broker.unbind_result()
    a = FakeAnalyze()
    r = env.runtime(a)
    rid, body = env.request(uid_off=4)
    broker.send(body, rid)
    assert _wait(lambda: r.consumer.paused == ["publish"]), "Return 인데 소비가 멈추지 않음"
    assert r.stats.snapshot().get("publish.result.returned", 0) >= 1
    assert _wait(lambda: broker.unacked(REQ) == 1, 12) and r.handled == []      # 요청은 쥔 채(ACK 안 함)
    assert not r.consumer.status()["consuming"]
    broker.bind_result()
    (got,) = broker.take(RES, 1)
    assert json.loads(got[1])["requestId"] == rid and a.calls == 1               # 같은 바이트 재발행 — 재분석 없음
    assert _wait(lambda: r.handled == [Handled.ACKED]) and _wait(lambda: r.consumer.paused == [])
    assert _wait(lambda: r.consumer.status()["consuming"])


def test_분석_중_브로커_연결이_끊기면_재배달은_재분석_없이_저장_결과로(env, broker):
    a = FakeAnalyze()
    a.gate = threading.Event()
    r = env.runtime(a)
    rid, body = env.request(uid_off=5)
    broker.send(body, rid)
    assert _wait(lambda: a.calls == 1)
    assert broker.close_all_connections(at_least=2) >= 2           # 소비 · 발행 연결 둘 다 끊음
    assert _wait(lambda: r.consumer.status()["connected"] and r.publisher.connected)
    a.gate.set()                                                  # 첫 작업이 마저 끝냄 — 옛 delivery tag 로는 ACK 못 함
    res = broker.take(RES, 1)
    assert res and json.loads(res[0][1])["requestId"] == rid
    assert _wait(lambda: len(r.handled) == 2), r.handled
    assert sorted(r.handled) == sorted([Handled.ACKED, Handled.ACK_LOST]) or Handled.ABANDONED in r.handled
    assert a.calls == 1                                           # 재배달은 저장 결과를 다시 보낸다(재분석 없음)
    more = broker.take(RES, 1, timeout=1)
    assert all(b == res[0][1] for _p, b in more)                  # 중복 발행이 있어도 같은 바이트(BE 멱등 — BE-05)
    r.stop()
    assert broker.ready(REQ) == 0 and _row(env, rid).state == st.SETTLED


def test_정상_종료는_진행중_요청을_ACK_없이_돌려놓고_다음_기동이_재분석_없이_마저(env, broker):
    a = FakeAnalyze()
    a.gate = threading.Event()
    r = env.runtime(a, shutdown_grace_s=1)
    rid, body = env.request(uid_off=6)
    broker.send(body, rid)
    assert _wait(lambda: a.calls == 1)
    t0 = time.monotonic()
    r.stop()                                                      # 유예 1초 안에 안 끝남 → ACK 없이 연결 닫기
    assert time.monotonic() - t0 < 10
    assert _wait(lambda: broker.ready(REQ) == 1)                  # 브로커가 요청을 되돌렸다
    a.gate.set()                                                  # 옛 작업은 결과를 저장하지만 발행기가 꺼져 STOPPED
    assert _wait(lambda: r.handled == [Handled.STOPPED])
    r2 = env.runtime(a)
    (got,) = broker.take(RES, 1)
    assert json.loads(got[1])["requestId"] == rid and a.calls == 1
    assert _wait(lambda: r2.handled == [Handled.ACKED])


def test_prefetch_와_슬롯은_따로_제한(env, broker):
    a = FakeAnalyze()
    a.gate = threading.Event()
    r = env.runtime(a, slots=2, prefetch=4)
    reqs = [env.request(uid_off=10 + i) for i in range(6)]
    for rid, b in reqs:
        broker.send(b, rid)
    assert _wait(lambda: a.calls == 2)
    time.sleep(1.0)
    assert a.calls == 2 and a.peak == 2                           # 동시 분석 = 슬롯 2
    assert _wait(lambda: broker.unacked(REQ) == 4, 12)            # 받아 둔 수 = prefetch 4 (관리 API 집계는 몇 초 늦을 수 있다)
    assert broker.ready(REQ) == 2
    a.gate.set()
    got = broker.take(RES, 6, timeout=20)
    assert {json.loads(b)["requestId"] for _p, b in got} == {rid for rid, _b in reqs}
    assert _wait(lambda: r.handled.count(Handled.ACKED) == 6) and a.peak == 2


def test_health_mq_칸_실제_연결(env, broker):
    r = env.runtime(FakeAnalyze())
    h = r.health()
    assert h["enabled"] and h["consumer"]["connected"] and h["consumer"]["consuming"] and h["consumer"]["paused"] == []
    assert set(h["deliveries"]) >= {"open", "dlq_pending"}


def test_대조_복구_실제_브로커로_빠진_발행을_마저(env, broker):
    rid, body = env.request(uid_off=20)
    ds = env.deliveries
    row = ds.receive(parse_request(body, rid).body_sha256)
    ds.set_identity(row.delivery_id, request_id=rid, recipient_user_id=UID0 + 20, source_version=7, input_hash="h" * 64,
                    converted={"requestId": rid})
    ds.set_result(row.delivery_id, b'{"requestId":"' + rid.encode() + b'"}')
    with env.engine.begin() as c:
        c.execute(sa.text("update ai_profile.mq_deliveries set updated_at = now() - interval '2 hours' where delivery_id = :i"),
                  {"i": row.delivery_id})
    pub = Publisher.from_url(URL, confirm_timeout_s=5, reconnect_delay_s=0.3)
    pub.start()
    try:
        rep = rc.Reconciler(ds, pub, older_than_s=3600).run_once()
    finally:
        pub.stop()
    assert rep.counts["REPUBLISHED"] == 1
    (got,) = broker.take(RES, 1)
    assert got[0].message_id == rid and got[0].delivery_mode == 2
    assert ds.get(row.delivery_id).state == st.SETTLED


@pytest.mark.skipif(not os.environ.get("PROFILING_TEST_AMQP_SLOW"), reason="느린 시험(최대 ~2분) — PROFILING_TEST_AMQP_SLOW=1 로 켬")
def test_CO04_consumer_timeout_을_넘기면_소비자_취소_다시_연결_재배달_재분석_없음(env, broker):
    """분석(또는 재시도 대기)이 브로커 consumer timeout 보다 길 때. 기본값은 30분(서버 설정 consumer_timeout 1800000).
    11-08 실측(RabbitMQ 4.4 · quorum): 채널을 닫지 않고 소비자만 취소(basic.cancel) + 메시지를 ready 로 돌린다.
    소비자는 이를 연결 변경으로 보고 다시 연결해야 한다 — 그러지 않으면 '소비 중' 으로 보이며 영영 받지 않는다(이 시험이 처음 잡음)."""
    broker.set_policy("t-consumer-timeout", "^profiling[.]request$",
                      {"max-length": 10000, "max-length-bytes": 268435456, "overflow": "reject-publish", "consumer-timeout": 3000}, 50)
    try:
        a = FakeAnalyze()
        a.gate = threading.Event()
        r = env.runtime(a)
        rid, body = env.request(uid_off=30)
        broker.send(body, rid)
        assert _wait(lambda: a.calls == 1)
        t0 = time.monotonic()
        gen = r.consumer._gen
        assert _wait(lambda: r.consumer._gen > gen, 120), "consumer timeout 뒤 다시 연결하지 않음"
        took = time.monotonic() - t0
        a.gate.set()
        (got,) = broker.take(RES, 1, timeout=20)
        assert json.loads(got[1])["requestId"] == rid
        assert _wait(lambda: Handled.ACKED in r.handled, 20) and a.calls == 1
        print(f"\n[CO-04] consumer-timeout 3초 설정 → 소비자 취소 → 다시 연결까지 {took:.1f}초, 처리 결과 {r.handled}")
    finally:
        broker.delete_policy("t-consumer-timeout")
