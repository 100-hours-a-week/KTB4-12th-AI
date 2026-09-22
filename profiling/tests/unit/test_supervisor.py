"""runtime/supervisor — 슬롯 1이면 동시에 하나만 돈다."""

import threading
import time

from fastapi import BackgroundTasks

from profiling.runtime.supervisor import Supervisor


def test_slots_limit_concurrency() -> None:
    sup = Supervisor(profiling_slots=1)
    peak = {"n": 0}; cur = {"n": 0}; lock = threading.Lock()

    def job():
        with lock:
            cur["n"] += 1; peak["n"] = max(peak["n"], cur["n"])
        time.sleep(0.05)
        with lock:
            cur["n"] -= 1

    bg = BackgroundTasks()
    for _ in range(4):
        sup.submit(bg, job)
    threads = [threading.Thread(target=t.func, args=t.args, kwargs=t.kwargs) for t in bg.tasks]   # 동시에 4개 시작
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert peak["n"] == 1                                      # 슬롯 1 → 겹치지 않음
    assert sup.stats() == {"slots": 1, "running": 0, "submitted": 4}
