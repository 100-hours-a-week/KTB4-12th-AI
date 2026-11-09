"""결과 · 실패 큐 발행기 (설계 §3 · §5 6단계 · 체크리스트 5).

설계 규칙:
- 기존 profiling.exchange 와 profiling.result · profiling.request.failed 라우팅을 쓴다. Persistent · JSON/UTF-8 · mandatory · publisher confirms.
- Confirm ACK 만으로 라우팅 성공을 판정하지 않는다 → mandatory 로 돌아온 메시지(Return)는 실패로 본다.
- 확인 타임아웃은 "성공 여부 불명" — 같은 바이트를 다시 보내도 된다(부른 쪽이 처리).
- AMQP 호출은 클라이언트가 허용하는 스레드에서 → 발행 전용 스레드 하나가 연결 · 채널을 쥐고, 작업 스레드는 일을 맡기고 기다린다.

결과: CONFIRMED(브로커 수락 + Return 없음) / RETURNED(라우팅 안 됨) / NACKED(브로커 거부) / UNKNOWN(확인 못 받음 · 연결 오류).
CONFIRMED 만 성공이다. 나머지는 부른 쪽이 저장 결과를 둔 채 새 소비를 멈추고 다시 보낸다.
"""
from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from enum import StrEnum

import pika
import pika.exceptions

from profiling.mq.config import EXCHANGE

log = logging.getLogger("profiling.mq.publisher")

RESULT_ROUTING_KEY = "profiling.result"
# 실패 큐 이름은 BE-08 사용자 확정(11-08 수정본): AI 요청 실패 = profiling.request.failed, BE 결과 실패 = profiling.result.failed(AI 는 안 씀).
# 큐 · 바인딩 선언은 BE/인프라가 한다(AI 는 선언하지 않음). AI 요청 실패 큐로 옮기는 방식(여기서는 직접 복사 발행)은 아직 공동 조율 대상.
REQUEST_FAILED_ROUTING_KEY = "profiling.request.failed"
_IDLE_S = 0.5                                               # 일이 없을 때 heartbeat 처리 간격


class PublishResult(StrEnum):
    CONFIRMED = "CONFIRMED"
    RETURNED = "RETURNED"
    NACKED = "NACKED"
    UNKNOWN = "UNKNOWN"


@dataclass
class _Job:
    routing_key: str
    body: bytes
    message_id: str | None
    correlation_id: str | None
    headers: dict | None
    done: Future


class Publisher:
    def __init__(self, connect: Callable[[], object], *, confirm_timeout_s: float, reconnect_delay_s: float) -> None:
        self._connect = connect
        self.confirm_timeout_s, self.reconnect_delay_s = confirm_timeout_s, reconnect_delay_s
        self._jobs: queue.Queue[_Job] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False

    @classmethod
    def from_url(cls, url: str, **kw) -> Publisher:
        return cls(lambda: pika.BlockingConnection(pika.URLParameters(url)), **kw)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mq-publisher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self.confirm_timeout_s + 5)

    def publish(self, routing_key: str, body: bytes, *, message_id: str | None, correlation_id: str | None = None,
                headers: dict | None = None) -> PublishResult:
        """발행하고 확인을 기다린다(최대 confirm_timeout_s). 어느 스레드에서 불러도 된다."""
        if self._stop.is_set():
            return PublishResult.UNKNOWN
        job = _Job(routing_key, body, message_id, correlation_id, headers, Future())
        self._jobs.put(job)
        try:
            return job.done.result(timeout=self.confirm_timeout_s)
        except FutureTimeout:
            log.warning("mq 발행 확인 시간 초과(%.0f초) routing_key=%s — 성공 여부 불명", self.confirm_timeout_s, routing_key)
            return PublishResult.UNKNOWN

    # ---- 발행 스레드
    def _run(self) -> None:
        while not self._stop.is_set():
            conn = None
            try:
                conn = self._connect()
                ch = conn.channel()
                ch.confirm_delivery()
                self.connected = True
                while not self._stop.is_set():
                    try:
                        job = self._jobs.get(timeout=_IDLE_S)
                    except queue.Empty:
                        conn.process_data_events(time_limit=0)          # heartbeat
                        continue
                    if job.done.done():                                  # 부른 쪽이 이미 시간 초과로 떠났다 — 그래도 보낸다(불명 → 재발행 허용)
                        pass
                    self._publish_one(ch, job)
            except pika.exceptions.AMQPError as e:
                log.warning("mq 발행 연결 끊김 · 실패: %s — %.1f초 뒤 다시 연결", type(e).__name__, self.reconnect_delay_s)
            except Exception as e:  # noqa: BLE001 — 발행 스레드가 죽으면 결과가 영영 안 나가므로 잡아서 다시 연결
                log.error("mq 발행 스레드 오류: %s", type(e).__name__)
            finally:
                self.connected = False
                if conn is not None:
                    try:
                        if conn.is_open:
                            conn.close()
                    except Exception as e:  # noqa: BLE001
                        log.debug("mq 발행 연결 닫기 실패: %s", type(e).__name__)
            self._stop.wait(self.reconnect_delay_s)
        self._fail_pending()

    def _publish_one(self, ch, job: _Job) -> None:
        props = pika.BasicProperties(content_type="application/json", content_encoding="UTF-8", delivery_mode=2,
                                     message_id=job.message_id, correlation_id=job.correlation_id, headers=job.headers)
        try:
            ch.basic_publish(exchange=EXCHANGE, routing_key=job.routing_key, body=job.body, properties=props, mandatory=True)
            result = PublishResult.CONFIRMED
        except pika.exceptions.UnroutableError:
            result = PublishResult.RETURNED
            log.error("mq 발행 Return(라우팅 안 됨) routing_key=%s — Binding 확인 필요", job.routing_key)
        except pika.exceptions.NackError:
            result = PublishResult.NACKED
            log.error("mq 발행 Nack routing_key=%s", job.routing_key)
        except pika.exceptions.AMQPError:
            _set(job.done, PublishResult.UNKNOWN)
            raise                                                        # 연결을 다시 만든다
        _set(job.done, result)

    def _fail_pending(self) -> None:
        while True:
            try:
                _set(self._jobs.get_nowait().done, PublishResult.UNKNOWN)
            except queue.Empty:
                return


def _set(f: Future, v: PublishResult) -> None:
    if not f.done():
        f.set_result(v)
