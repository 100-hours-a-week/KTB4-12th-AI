"""운영 상태 — 외부 LLM(태그 추출기)이 지금 어떤지 메모리에 모아 /health 에 보인다(10-15, 인프라 요구 §4 "외부 AI 장애와 프로세스 장애 구분").

외부 LLM 이 죽어도 **프로세스는 정상**이고 요청도 실패하지 않는다 — 태그 없이 30개를 고르고 7.7 을 보낸다(DR-037).
그래서 /readyz 에는 넣지 않는다(넣으면 ECS 가 모든 Task 를 트래픽에서 빼 서비스 전체가 멈춘다). 대신 /health 의 `llm` 칸에서
"우리 문제인지(재시작) 외부 문제인지(기다림 · 키 · 요금 확인)"를 바로 구분하게 한다.

  state   off      추출기를 안 켰다(TAGGER=none) 또는 켜려 했으나 못 켰다(reason: no_api_key …)
          unknown  켜져 있지만 아직 한 번도 부르지 않았다
          ok       마지막 호출이 성공
          degraded 최근 호출이 실패(연속 1~2번)
          down     연속 DOWN_AFTER 번 이상 실패
`last_error` 는 실패 **종류**만(`fail:timeout` · `fail:http_429` · `bad_json` …) — 오류 원문 · 키 · 제공자 응답은 넣지 않는다(/health 는 인증 없음).
프로세스(Task)마다 따로 센다 — Task 가 여럿이면 각자 자기 값이다.
"""

from __future__ import annotations

import threading
from collections import Counter
from datetime import UTC, datetime
from typing import Any

DOWN_AFTER = 3
_FAIL_OUTPUT = ("bad_json", "no_tags_list")      # LLM 이 답은 했지만 형식이 깨짐 — 실패로 센다


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LlmStatus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._configured: dict[str, Any] = {"requested": "none", "enabled": False, "model": None, "reason": None}
            self._last: str | None = None
            self._last_ok_at: str | None = None
            self._last_error: str | None = None
            self._last_error_at: str | None = None
            self._consecutive = 0
            self._counts: Counter = Counter()

    def configure(self, *, requested: str, enabled: bool, model: str | None = None, reason: str | None = None) -> None:
        """기동 때 한 번 — 무엇을 켜려 했고 실제로 켜졌는지."""
        with self._lock:
            self._configured = {"requested": requested, "enabled": enabled, "model": model, "reason": reason}

    def record(self, tag_status: str) -> None:
        """분석 한 건의 태그 단계 결과(pipeline 의 tag_status). LLM 을 부르지 않은 상태(skip · skip_no_signal · off)는 세지 않는다."""
        if tag_status in ("skip", "skip_no_signal", "off"):
            return
        failed = tag_status.startswith("fail:") or tag_status in _FAIL_OUTPUT
        with self._lock:
            self._last = tag_status
            self._counts["fail" if failed else "ok"] += 1
            if failed:
                self._consecutive += 1
                self._last_error, self._last_error_at = tag_status, _now()
            else:
                self._consecutive = 0
                self._last_ok_at = _now()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            c = self._configured
            if not c["enabled"]:
                state = "off"
            elif self._last is None:
                state = "unknown"
            elif self._consecutive >= DOWN_AFTER:
                state = "down"
            elif self._consecutive > 0:
                state = "degraded"
            else:
                state = "ok"
            return {"state": state, **c, "last": self._last, "last_ok_at": self._last_ok_at, "last_error": self._last_error,
                    "last_error_at": self._last_error_at, "consecutive_failures": self._consecutive,
                    "calls": {"ok": self._counts["ok"], "fail": self._counts["fail"]}}


LLM = LlmStatus()
