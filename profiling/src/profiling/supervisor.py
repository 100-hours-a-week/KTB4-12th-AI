"""Supervisor — 프로파일링 실행의 수용량·소유·종료 (3단계 도표의 "접수 한도 · 작업 참조 · 종료 정리" 자리).

v1 (09-28 개정): **워커 스레드 N개 + 상한 있는 큐.**
  접수(7.6)는 submit() 으로 큐에 넣고 곧바로 202. 큐가 가득이거나 종료 중이면 False → 라우터가 503 으로 거절한다.
  Backend 는 503 을 재시도 가능 실패로 보고 틱을 끝내며 디바운스를 1회만 재시작한다(통합 수정점 v0.7 ②③, 10-08 실측) —
  그래서 큐 상한은 BE 틱당 일반 ⚙100 + 복구 ⚙50 보다 커야 한다. 실행은 전용 워커 스레드가 큐에서 꺼내 한 건씩 한다.

왜 바꿨나: 전에는 FastAPI BackgroundTasks 로 넘겨 anyio 스레드풀(토큰 40)에서 세마포어를 **쥔 채** 기다렸다. 슬롯이 느리면
대기 작업이 토큰을 다 잡아 /health 와 7.6 접수의 동기 의존성까지 같은 줄 뒤에 서서 굶었다(부하_시험_결과_2026-09-28 §6·§11:
/health 150초 무응답, 접수 34.5초). 지금은 대기가 큐에서 일어나 토큰을 쓰지 않는다.

규칙 (위키 서비스-아키텍처-구현-상세 §5.2·§10.2·§10.5):
  - 접수는 즉시 수용량(큐 자리)을 예약하고, 없으면 기다리지 않고 거절한다(7.6 에는 429 가 없어 503).
  - 접수 건마다 강한 참조(큐 항목)를 두고, 예외는 건마다 격리하며(워커가 죽지 않는다), 슬롯은 정확히 한 번 반환한다.
  - 종료: 신규 접수를 막고, 큐에 남은 것은 버리며(Backend 가 maximum-window 뒤 같은 번호로 복구 전송), 실행 중인 것은
    timeout 까지만 기다린다(compose 종료 유예 10초 안).
  - 영속 큐·DB 폴링은 없다 — profile_runs 는 큐가 아니다.
기한(profile_deadline 240초)·취소는 다음 단계.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

_Item = tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]


class Supervisor:
    """워커 스레드 + 상한 큐. main.lifespan 에서 1개 만들어 start() 하고 app.state.supervisor 에 둔다.

    submit(fn, *args) → True(큐에 들어감) / False(가득·종료 중). 워커가 fn(*args) 를 한 건씩 실행한다.
    stats() 의 queued 가 "접수 대기 건수"(위키 KPI) 다.
    """

    def __init__(self, profiling_slots: int = 1, queue_max: int = 200) -> None:
        if profiling_slots < 1:
            raise ValueError("profiling_slots는 1 이상")
        if queue_max < 1:
            raise ValueError("queue_max는 1 이상")
        self.profiling_slots = profiling_slots
        self.queue_max = queue_max
        self._q: queue.Queue[_Item | None] = queue.Queue(maxsize=queue_max)
        self._workers: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._closing = False
        self._running = 0
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._rejected = 0

    # ---- 수명 -------------------------------------------------------------------

    def start(self) -> None:
        """워커 스레드를 띄운다(슬롯 수만큼). 두 번 불러도 한 번만."""
        if self._workers:
            return
        for i in range(self.profiling_slots):
            th = threading.Thread(target=self._worker, name=f"profiling-worker-{i + 1}", daemon=True)
            th.start()
            self._workers.append(th)
        log.info("Supervisor 시작 slots=%d queue_max=%d", self.profiling_slots, self.queue_max)

    def stop(self, timeout_s: float = 5.0) -> int:
        """신규 접수를 막고 큐를 비운 뒤 워커를 멈춘다. 버린(실행하지 않은) 건수를 돌려준다.

        실행 중인 작업은 timeout_s 까지 기다린다 — 그 안에 안 끝나면 프로세스 종료와 함께 중단되고,
        그 RUNNING 행은 다음 기동의 recover_stale_runs 가 FAILED 로 정리한다.
        """
        with self._lock:
            self._closing = True
        dropped = 0
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                dropped += 1
            self._q.task_done()
        if dropped:
            log.warning("Supervisor 종료: 큐에 남은 %d건을 버린다 — Backend 가 maximum-window 뒤 같은 번호로 복구 전송한다", dropped)
        deadline = time.monotonic() + timeout_s
        for _ in self._workers:
            try:
                self._q.put(None, timeout=max(0.0, deadline - time.monotonic()))
            except queue.Full:
                break
        for th in self._workers:
            th.join(max(0.0, deadline - time.monotonic()))
        alive = sum(th.is_alive() for th in self._workers)
        if alive:
            log.warning("Supervisor 종료: 실행 중인 작업 %d개가 %.1f초 안에 끝나지 않음 — 프로세스와 함께 중단", alive, timeout_s)
        return dropped

    # ---- 접수 -------------------------------------------------------------------

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        """큐에 즉시 넣는다. 가득이거나 종료 중이면 False — 기다리지 않는다(호출자가 503 으로 거절)."""
        with self._lock:
            if self._closing:
                self._rejected += 1
                return False
            try:
                self._q.put_nowait((fn, args, kwargs))
            except queue.Full:
                self._rejected += 1
                log.warning("Supervisor 거절: 대기열 가득(%d) — 503 Retry-After 로 되돌린다", self.queue_max)
                return False
            self._submitted += 1
            return True

    # ---- 실행 -------------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:                                   # 종료 신호
                self._q.task_done()
                return
            fn, args, kwargs = item
            with self._lock:
                self._running += 1
            try:
                fn(*args, **kwargs)
            except Exception:                                  # 한 건의 예외가 워커를 죽이지 않는다(격리)
                log.exception("Supervisor: 작업 예외 — 다음 작업으로")
                with self._lock:
                    self._failed += 1
            finally:
                with self._lock:
                    self._running -= 1
                    self._completed += 1
                self._q.task_done()

    def run_pending(self) -> int:
        """워커 없이 큐를 **이 스레드에서** 비운다(단위 시험·도구용). 처리한 건수."""
        n = 0
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                return n
            if item is None:
                self._q.task_done()
                continue
            fn, args, kwargs = item
            try:
                fn(*args, **kwargs)
            except Exception:
                log.exception("Supervisor.run_pending: 작업 예외")
                with self._lock:
                    self._failed += 1
            finally:
                with self._lock:
                    self._completed += 1
                self._q.task_done()
                n += 1

    # ---- 관측 (health·시험용) ----------------------------------------------------

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"slots": self.profiling_slots, "running": self._running, "queued": self._q.qsize(),
                    "queue_max": self.queue_max, "submitted": self._submitted, "completed": self._completed,
                    "rejected": self._rejected, "failed": self._failed}
