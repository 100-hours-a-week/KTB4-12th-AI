"""supervisor — 워커 스레드 + 상한 큐 (09-28). 대기가 스레드풀 토큰을 쓰지 않고, 가득이면 즉시 거절한다."""

import threading
import time

from profiling.supervisor import Supervisor


def _until(cond, timeout: float = 3.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_slots_limit_concurrency() -> None:
    """슬롯 1 → 워커 1개 → 네 작업이 겹치지 않는다. 큐에 넣는 순간 202 가 나가고(제출 4) 실행은 순서대로."""
    sup = Supervisor(profiling_slots=1, queue_max=10)
    sup.start()
    peak = {"n": 0}; cur = {"n": 0}; lock = threading.Lock()

    def job():
        with lock:
            cur["n"] += 1; peak["n"] = max(peak["n"], cur["n"])
        time.sleep(0.05)
        with lock:
            cur["n"] -= 1

    try:
        for _ in range(4):
            assert sup.submit(job) is True
        assert _until(lambda: sup.stats()["completed"] == 4)
        assert peak["n"] == 1                                      # 슬롯 1 → 겹치지 않음
        s = sup.stats()
        assert s == {"slots": 1, "running": 0, "queued": 0, "queue_max": 10, "submitted": 4, "completed": 4, "rejected": 0, "failed": 0}
    finally:
        sup.stop(1.0)


def test_queue_full_rejects_immediately_without_waiting() -> None:
    """워커를 띄우지 않으면 큐만 찬다 — 상한을 넘는 제출은 기다리지 않고 False(라우터가 503)."""
    sup = Supervisor(profiling_slots=1, queue_max=2)
    assert sup.submit(lambda: None) is True and sup.submit(lambda: None) is True
    t = time.monotonic()
    assert sup.submit(lambda: None) is False
    assert time.monotonic() - t < 0.1
    assert sup.stats()["queued"] == 2 and sup.stats()["rejected"] == 1 and sup.stats()["submitted"] == 2
    assert sup.run_pending() == 2                                 # 시험용: 이 스레드에서 비운다
    assert sup.stats()["queued"] == 0 and sup.stats()["completed"] == 2


def test_exception_in_one_job_does_not_kill_the_worker() -> None:
    sup = Supervisor(profiling_slots=1, queue_max=10)
    sup.start()
    done = threading.Event()

    def bad():
        raise RuntimeError("boom")

    try:
        assert sup.submit(bad) and sup.submit(done.set)
        assert done.wait(3.0)                                       # 예외 뒤에도 다음 작업이 돈다
        assert _until(lambda: sup.stats()["completed"] == 2)
        assert sup.stats()["failed"] == 1 and sup.stats()["running"] == 0
    finally:
        sup.stop(1.0)


def test_stop_rejects_new_work_drops_queued_and_waits_for_running() -> None:
    sup = Supervisor(profiling_slots=1, queue_max=10)
    sup.start()
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5.0)

    assert sup.submit(slow) and started.wait(3.0)
    assert sup.submit(lambda: None) and sup.submit(lambda: None)   # 큐에 2건 — 실행 전
    result: list[int] = []
    stopper = threading.Thread(target=lambda: result.append(sup.stop(3.0)))
    stopper.start()
    assert _until(lambda: sup.submit(lambda: None) is False)      # closing → 거절
    release.set()                                                  # 실행 중이던 것이 끝난다
    stopper.join(5.0)
    assert result == [2]                                           # 버린 건수 = 큐에 있던 2
    assert _until(lambda: not any(th.is_alive() for th in sup._workers))
    assert sup.stats()["running"] == 0


def test_invalid_sizes_are_refused() -> None:
    import pytest
    with pytest.raises(ValueError):
        Supervisor(profiling_slots=0)
    with pytest.raises(ValueError):
        Supervisor(profiling_slots=1, queue_max=0)
