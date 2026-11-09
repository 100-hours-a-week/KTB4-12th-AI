"""요청 Consumer 뼈대 — 수동 ACK · 소비 슬롯 · 다시 연결 · 멈춤/재개 · 정상 종료 (설계 §5 · §8 체크리스트 2).

설계에서 가져온 규칙:
- 브로커 push 소비 + 수동 ACK. 유한 Prefetch. Prefetch 와 실제 분석 동시성(슬롯)은 따로 제한한다.
  → basic.qos(prefetch) 가 "받아 둘 수"를, 슬롯 수만큼의 작업 스레드가 "동시에 분석하는 수"를 막는다.
    받아 둔 메시지는 prefetch 를 넘을 수 없으므로 내부 대기열도 무제한이 되지 않는다.
- 연결 · 카탈로그 · DB 준비를 확인한 뒤 소비를 시작한다 → `ready()` 가 참일 때만 basic.consume.
- AI 는 큐를 선언하지 않는다(BE/인프라 책임) → 시작 때 passive 확인만.
- ACK 는 메시지별로, 그 메시지를 받은 채널에서. AMQP 호출은 클라이언트가 허용하는 스레드에서 한다
  → pika 연결 · 채널은 IO 스레드 하나만 만지고, 작업 스레드는 add_callback_threadsafe 로 ACK 를 맡긴다.
  분석은 작업 스레드에서 돌아 heartbeat(IO 스레드)를 막지 않는다.
- 브로커 연결이 끊기면 받은 delivery tag 를 버린다(옛 연결의 ACK 는 보내지 않음). 재연결 뒤 재배달을 받는다.
  브로커가 소비자만 취소해도(basic.cancel — consumer timeout 초과 · 큐 삭제) 같게 다룬다(BrokerCancelled → 다시 연결).
- 결과 · DLQ 발행이 안 되면 새 소비를 일시 중지한다 → `pause(이유)` / `resume(이유)`(받은 메시지는 쥔 채로).
  멈춤은 이유별 개수로 센다 — 한 작업이 풀어도 다른 작업이 건 멈춤은 남는다(모두 풀려야 재개).
- 종료: 새 소비 중단 → 진행 중 작업을 유예 시간까지 기다림 → 연결을 닫아 미ACK 요청이 재배달되게 한다.

이 파일은 "어떻게 받고 돌려주는가" 만 맡는다. 메시지 하나를 어떻게 처리하는지(잠금 · 분석 · 발행 · ACK 시점)는 handler 가 정한다.
handler 가 예외를 내면 로그만 남기고 ACK 하지 않는다 — 메시지는 연결이 바뀌거나 재시작될 때 재배달된다(조용히 버리지 않음).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pika
import pika.exceptions

from profiling.mq.config import REQUEST_QUEUE, MqConfig

log = logging.getLogger("profiling.mq.consumer")

_TICK_S = 0.2           # IO 스레드가 한 번에 이벤트를 기다리는 시간 — 멈춤 · 종료 · 준비 신호를 이 간격으로 본다


class BrokerCancelled(Exception):
    """브로커가 소비자를 취소했다(basic.cancel). 채널 · 연결은 열려 있어도 더는 메시지가 오지 않는다.
    11-08 실제 브로커(RabbitMQ 4.4 · quorum) 실측: consumer timeout 을 넘기면 채널을 닫지 않고 소비자만 취소하고,
    쥐고 있던 메시지는 다시 ready 로 돌린다(큐 삭제 때도 같은 취소가 온다).
    → 연결이 바뀐 것과 똑같이 다룬다: 받은 delivery tag 를 버리고(세대 +1) 다시 연결해 재배달을 받는다."""


@dataclass
class Delivery:
    """작업 스레드에 넘기는 메시지 한 건."""

    body: bytes
    message_id: str | None
    delivery_tag: int
    redelivered: bool
    _ack: Callable[[], bool] = field(repr=False)
    _alive: Callable[[], bool] = field(default=lambda: True, repr=False)

    def ack(self) -> bool:
        """이 메시지를 ACK 하도록 IO 스레드에 맡긴다. 받은 연결이 이미 끊겼으면 False(그 tag 는 버려졌다 — 재배달된다)."""
        return self._ack()

    @property
    def stale(self) -> bool:
        """받은 연결이 이미 바뀌었다 — 이 delivery tag 로는 ACK 할 수 없다(설계 §5 '기존 delivery tag 폐기').
        기다리던 작업(잠금 · 재시도 · 발행 재시도)은 그만두고 슬롯을 비운다. 재배달된 것이 영속 기록으로 이어 간다."""
        return not self._alive()


class RequestConsumer:
    def __init__(self, cfg: MqConfig, handler: Callable[[Delivery], None], *,
                 ready: Callable[[], bool] = lambda: True,
                 connect: Callable[[], object] | None = None,
                 queue: str = REQUEST_QUEUE) -> None:
        self.cfg, self.handler, self.ready, self.queue = cfg, handler, ready, queue
        self._connect = connect or (lambda: pika.BlockingConnection(pika.URLParameters(cfg.url)))
        self._pool = ThreadPoolExecutor(max_workers=cfg.slots, thread_name_prefix="mq-slot")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._io: threading.Thread | None = None
        self._gen = 0                   # 연결 세대 — 끊길 때마다 +1, 옛 세대의 ACK 는 버린다
        self._inflight = 0
        self._pauses: Counter[str] = Counter()     # 멈춤 이유별로 건 작업 수 — 0 이 되면 그 이유는 풀린다
        self.connected = False
        self.consuming = False
        self.last_message_at: datetime | None = None

    # ---- 밖에서 부르는 것
    def start(self) -> None:
        self._io = threading.Thread(target=self._run, name="mq-io", daemon=True)
        self._io.start()

    def pause(self, reason: str) -> None:
        """한 작업이 한 이유로 한 번 부른다(같은 작업이 같은 이유로 두 번 부르지 않음 — process._Hold 가 지킨다)."""
        with self._lock:
            if not self._pauses:
                log.warning("mq 소비 일시 중지: %s", reason)
            self._pauses[reason] += 1

    def resume(self, reason: str) -> None:
        with self._lock:
            if self._pauses[reason] > 0:
                self._pauses[reason] -= 1
            self._pauses = +self._pauses              # 0 인 이유는 지운다
            if not self._pauses:
                log.info("mq 소비 재개 (마지막으로 풀린 이유: %s)", reason)

    @property
    def paused(self) -> list[str]:
        with self._lock:
            return sorted(self._pauses)

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def wait(self, seconds: float) -> bool:
        """작업 스레드가 '제한 간격 대기'(잠금 · DB · 발행 재시도)에 쓴다. 종료가 시작되면 바로 True(기다림을 그만둘 것)."""
        return self._stop.wait(seconds)

    def stop(self) -> None:
        """새 소비 중단 → 진행 중 작업을 유예 시간까지 기다림 → 연결 닫기. IO 스레드가 끝날 때까지 돌아오지 않는다."""
        self._stop.set()
        if self._io is not None:
            self._io.join(self.cfg.shutdown_grace_s + 10)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def status(self) -> dict:
        with self._lock:
            return {"connected": self.connected, "consuming": self.consuming, "paused": sorted(self._pauses),
                    "inflight": self._inflight, "slots": self.cfg.slots, "prefetch": self.cfg.prefetch,
                    "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None}

    # ---- IO 스레드
    def _run(self) -> None:
        while not self._stop.is_set():
            conn = None
            try:
                conn = self._connect()
                ch = conn.channel()
                ch.basic_qos(prefetch_count=self.cfg.prefetch)
                ch.queue_declare(queue=self.queue, passive=True)     # 준비 확인만 — 선언(인자 지정)은 하지 않는다
                with self._lock:
                    self._gen += 1
                    gen = self._gen
                    self.connected = True
                log.info("mq 연결됨 (세대 %d, prefetch %d, 슬롯 %d)", gen, self.cfg.prefetch, self.cfg.slots)
                self._loop(conn, ch, gen)
                self._drain(conn)
                return
            except BrokerCancelled:
                log.warning("mq 브로커가 소비를 취소함(consumer timeout · 큐 삭제 등) — 받은 메시지는 재배달 대기, %.1f초 뒤 다시 연결",
                            self.cfg.reconnect_delay_s)
            except pika.exceptions.AMQPError as e:
                log.warning("mq 연결 끊김 · 실패: %s — %.1f초 뒤 다시 연결", type(e).__name__, self.cfg.reconnect_delay_s)
            except Exception as e:  # noqa: BLE001 — IO 스레드가 죽으면 소비가 조용히 멈추므로 잡아서 다시 연결한다
                log.error("mq IO 오류: %s — %.1f초 뒤 다시 연결", type(e).__name__, self.cfg.reconnect_delay_s)
            finally:
                with self._lock:
                    self._gen += 1                       # 받은 delivery tag 는 이제 쓸 수 없다
                    self.connected = self.consuming = False
                self._close(conn)
            self._stop.wait(self.cfg.reconnect_delay_s)

    def _loop(self, conn, ch, gen: int) -> None:
        tag = None
        cancelled: list[str] = []
        ch.add_on_cancel_callback(lambda frame: cancelled.append(getattr(frame.method, "consumer_tag", "?")))
        while not self._stop.is_set():
            if cancelled:
                raise BrokerCancelled(cancelled[0])
            with self._lock:
                paused = bool(self._pauses)
            want = not paused and self._is_ready()
            if want and tag is None:
                tag = ch.basic_consume(queue=self.queue, on_message_callback=self._on_message(conn, ch, gen), auto_ack=False)
            elif not want and tag is not None:
                ch.basic_cancel(tag)
                tag = None
            with self._lock:
                self.consuming = tag is not None
            conn.process_data_events(time_limit=_TICK_S)
        if tag is not None:                              # 종료 1단계: 새 소비 중단
            ch.basic_cancel(tag)
            with self._lock:
                self.consuming = False

    def _drain(self, conn) -> None:
        """종료 2단계: 진행 중 작업이 끝나 ACK 를 맡길 때까지 IO 를 계속 돌린다(유예 시간까지). 3단계 연결 닫기는 _run 의 finally."""
        deadline = time.monotonic() + self.cfg.shutdown_grace_s
        while time.monotonic() < deadline:
            with self._lock:
                if self._inflight == 0:
                    break
            conn.process_data_events(time_limit=_TICK_S)
        with self._lock:
            left = self._inflight
        if left:
            log.warning("mq 종료 유예 %.0f초 초과 — 진행 중 %d건은 ACK 없이 연결을 닫는다(브로커가 재배달)", self.cfg.shutdown_grace_s, left)
        else:
            conn.process_data_events(time_limit=0)       # 마지막으로 맡겨진 ACK 를 보낸다

    def _is_ready(self) -> bool:
        try:
            return bool(self.ready())
        except Exception as e:  # noqa: BLE001 — 준비 확인이 실패하면 준비 안 된 것으로 본다
            log.warning("mq 준비 확인 실패: %s", type(e).__name__)
            return False

    def _on_message(self, conn, ch, gen: int):
        def on_message(_ch, method, properties, body: bytes) -> None:
            def ack() -> bool:
                with self._lock:
                    if gen != self._gen:
                        return False
                try:
                    conn.add_callback_threadsafe(lambda: self._ack_now(ch, gen, method.delivery_tag))
                    return True
                except Exception:  # noqa: BLE001 — 연결이 이미 닫혔다
                    return False

            def alive() -> bool:
                with self._lock:
                    return gen == self._gen

            d = Delivery(body=body, message_id=getattr(properties, "message_id", None), delivery_tag=method.delivery_tag,
                         redelivered=bool(method.redelivered), _ack=ack, _alive=alive)
            with self._lock:
                self._inflight += 1
                self.last_message_at = datetime.now(UTC)
            self._pool.submit(self._work, d)
        return on_message

    def _ack_now(self, ch, gen: int, tag: int) -> None:
        with self._lock:
            if gen != self._gen:
                return
        if ch.is_open:
            ch.basic_ack(delivery_tag=tag)

    def _work(self, d: Delivery) -> None:
        try:
            self.handler(d)
        except Exception as e:  # noqa: BLE001 — 입력이 실릴 수 있는 traceback 은 남기지 않는다
            log.error("mq 처리 예외 delivery_tag=%d: %s — ACK 하지 않음(재배달 대기)", d.delivery_tag, type(e).__name__)
        finally:
            with self._lock:
                self._inflight -= 1

    @staticmethod
    def _close(conn) -> None:
        if conn is None:
            return
        try:
            if conn.is_open:
                conn.close()
        except Exception as e:  # noqa: BLE001 — 닫다가 실패해도 브로커가 연결을 정리한다
            log.debug("mq 연결 닫기 실패: %s", type(e).__name__)
