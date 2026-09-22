"""Supervisor — 3단계 도표의 "접수 한도 · 작업 참조 · 기한 · 취소 · 종료 정리 · 단일 실행 소유권" 자리.

v1은 이 중 **프로파일링 슬롯(동시 실행 수 제한)** 만 구현한다. 접수(7.6)는 202로 즉시 돌아가고, 실제 분석은 이 Supervisor가
백그라운드로 넘긴다. 슬롯이 다 차 있으면 기다렸다가 순서대로 실행한다(거절하지 않음 — Backend가 재시도하게 만드는 것보다 늦게라도
처리하는 편이 v1에 맞다). 기한(timeout)·취소·재시작 복구는 DB 실행 기록(ProfileRunStore)과 함께 다음 단계(#18).

3단계 구현 상세 §12.1의 개발 시험 시작값: 프로파일링 슬롯 1, 전체 기한 240초 → 슬롯만 지금, 기한은 다음.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from fastapi import BackgroundTasks

log = logging.getLogger(__name__)


class Supervisor:
    """프로파일링 실행을 슬롯으로 제한하는 감독자. main.lifespan에서 1개 만들어 app.state.supervisor에 둔다.

    submit(bg, fn, *args): FastAPI BackgroundTasks에 등록하되, 실제 실행 시점에 세마포어를 잡고 fn을 부른다.
    BackgroundTasks는 응답 뒤 스레드풀에서 돌므로 threading.Semaphore로 충분하다.
    """

    def __init__(self, profiling_slots: int = 1) -> None:
        if profiling_slots < 1:
            raise ValueError("profiling_slots는 1 이상")
        self.profiling_slots = profiling_slots
        self._sem = threading.Semaphore(profiling_slots)
        self._lock = threading.Lock()
        self._running = 0
        self._submitted = 0

    def submit(self, bg: BackgroundTasks, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """응답이 나간 뒤 fn(*args, **kwargs)을 슬롯 안에서 실행하도록 등록한다."""
        with self._lock:
            self._submitted += 1
        bg.add_task(self._run, fn, *args, **kwargs)

    def _run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        with self._sem:  # 슬롯 확보 — 차 있으면 여기서 대기
            with self._lock:
                self._running += 1
            try:
                fn(*args, **kwargs)
            finally:
                with self._lock:
                    self._running -= 1

    # ---- 관측 (health·시험용) --------------------------------------------------

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"slots": self.profiling_slots, "running": self._running, "submitted": self._submitted}
