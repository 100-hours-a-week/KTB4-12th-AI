"""Profile store (메모리) — recipient_user_id → ProfileOutcome. DB adapter(profile_runs·recipient_profiles) 전까지의 임시.

DB adapter로 바꿀 때 유지할 계약(ports.ProfileRunStore):
  save(outcome)             같은 수신자는 덮어쓴다 (upsert). 예외를 내지 않는다.
  get(recipient_user_id)    마지막 결과 또는 None.

오늘 하지 않는 것 (DB에서 할 것):
  - source_version 순서 역전 — 낮은 버전이 뒤에 오면 그냥 덮어쓴다. 409·SUPERSEDED 규칙은 개발 이슈 #23.
  - 실행 기록(profile_runs)과 프로필(recipient_profiles)의 분리 — 여기서는 ProfileOutcome 하나에 다 들어 있다.
  - 프로세스 재시작 후 보존 — 메모리라 사라진다.

스레드 안전: FastAPI BackgroundTasks가 같은 프로세스의 스레드풀에서 돌 수 있으므로 Lock 하나로 dict를 지킨다.
"""

from __future__ import annotations

import logging
import threading

from profiling.profile.types import ProfileOutcome

log = logging.getLogger(__name__)


class MemoryProfileRunStore:
    """ports.ProfileRunStore 구현."""

    def __init__(self) -> None:
        self._items: dict[int, ProfileOutcome] = {}
        self._lock = threading.Lock()

    # ---- ports.ProfileRunStore --------------------------------------------------
    ## source_version check
    def save(self, outcome: ProfileOutcome) -> None:
        rid = outcome.recipient_user_id
        with self._lock:
            prev = self._items.get(rid)
            self._items[rid] = outcome
        if prev is not None and prev.source_version > outcome.source_version:
            # 순서 역전 — DB adapter에서는 저장하지 않고 SUPERSEDED로 표시할 자리(#23). 오늘은 기록만.
            log.warning("MemoryProfileRunStore: recipient=%s source_version 역전 %s → %s (덮어씀)", rid, prev.source_version, outcome.source_version)
        else:
            log.debug("MemoryProfileRunStore: recipient=%s source_version=%s status=%s 저장", rid, outcome.source_version, outcome.status)

    def get(self, recipient_user_id: int) -> ProfileOutcome | None:
        with self._lock:
            return self._items.get(recipient_user_id)

    # ---- 편의 (시험·디버그) ------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
