"""태그 추출 대역(성능 측정 전용) — 모델을 부르지 않고, 정한 응답 시간만큼 기다렸다가 고정 답 `{"tags": []}` 를 돌려준다.

PROFILING_TAGGER_STUB=true · PROFILING_TAGGER=openrouter 일 때만 켜진다(기본 꺼짐). OpenRouter 추출기의 **전송만** 이것으로 바꾼다 —
요청 만들기 · 응답 해석 · 태그 검증 · 검색 · 저장 · 결과 전달은 실제 코드 그대로 돈다. 바깥으로 나가는 연결이 없으므로 키가 필요 없고
비용도 0 이다(키를 넣어도 이 전송 밖으로 나가지 않는다).

응답 시간 — p50 · p95(초)와 흔들림 방식(TAGGER_STUB_JITTER)으로 정한다. 기본값은 v3 운영 앱(Modal A100) 8명 동시 실측
(11-04 300명: p50 4.1 · p95 6.9 · 가장 느린 한 명 12.8초 — 팀 폴더 AI/프로파일러_v3_정리_2026-11-04 README).
  none       늘 p50
  lognormal  로그정규 — 중앙값 p50, 95번째 값 p95(실제 응답 시간처럼 오른쪽 꼬리가 긴 모양). 기본
  uniform    균등 — 중앙값 p50, 95번째 값 p95 가 되게 [p50 − w, p50 + w], w = (p95 − p50) / 0.9 (0 아래는 0)
어느 방식이든 TAGGER_STUB_MAX_S 로 자른다. TAGGER_STUB_SEED 를 주면 뽑는 순서가 매번 같다(동시 처리 순서는 다를 수 있음).

운영 차단 — AI_ENVIRONMENT=production 이면 켜지 않고 StubRefused(서버 기동 실패). 운영에서 실수로 켜져 빈 태그가 나가는 일을 막는다.
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
import time
from typing import Literal

import httpx

Jitter = Literal["none", "lognormal", "uniform"]
STUB_MODEL = "stub/v3-latency"          # 실행 기록 · 로그에 남는 모델 이름 — 실제 모델 결과로 오해하지 않게
_Z95 = 1.6448536269514722               # 표준정규 95번째 값
STUB_REPLY = '{"tags": []}'


class StubDelay:
    """응답 시간 뽑기(스레드 안전)."""

    def __init__(self, p50_s: float, p95_s: float, jitter: Jitter = "lognormal", max_s: float | None = None,
                 seed: int | None = None) -> None:
        if not p50_s > 0:
            raise ValueError("TAGGER_STUB_P50_S 는 0 보다 커야 함")
        if jitter not in ("none", "lognormal", "uniform"):
            raise ValueError(f"TAGGER_STUB_JITTER 는 none · lognormal · uniform 중 하나: {jitter!r}")
        if jitter != "none" and not p95_s >= p50_s:
            raise ValueError("TAGGER_STUB_P95_S 는 TAGGER_STUB_P50_S 이상이어야 함")
        if max_s is not None and not max_s >= p50_s:
            raise ValueError("TAGGER_STUB_MAX_S 는 TAGGER_STUB_P50_S 이상이어야 함")
        self.p50_s, self.p95_s, self.jitter, self.max_s = p50_s, p95_s, jitter, max_s
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    def sample(self) -> float:
        with self._lock:
            if self.jitter == "none":
                d = self.p50_s
            elif self.jitter == "lognormal":
                sigma = math.log(self.p95_s / self.p50_s) / _Z95
                d = self._rng.lognormvariate(math.log(self.p50_s), sigma)
            else:
                w = (self.p95_s - self.p50_s) / 0.9
                d = max(0.0, self._rng.uniform(self.p50_s - w, self.p50_s + w))
        return min(d, self.max_s) if self.max_s is not None else d

    def describe(self) -> str:
        cap = f" max={self.max_s}s" if self.max_s is not None else ""
        return f"jitter={self.jitter} p50={self.p50_s}s p95={self.p95_s}s{cap}"


class StubTransport(httpx.BaseTransport):
    """OpenRouter /chat/completions 자리 — 바깥 연결 없이 sample() 초 기다린 뒤 고정 답(사용량 0)."""

    def __init__(self, delay: StubDelay, sleep=time.sleep) -> None:
        self.delay, self._sleep = delay, sleep

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._sleep(self.delay.sample())
        body = {"choices": [{"message": {"content": STUB_REPLY}, "finish_reason": "stop"}],
                "usage": {"cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0}}
        return httpx.Response(200, content=json.dumps(body).encode(), headers={"content-type": "application/json"},
                              request=request)


BLOCKED_ENVIRONMENTS = frozenset({"production"})


class StubRefused(RuntimeError):
    """운영 환경에서 대역을 켜려 할 때 — 서버가 뜨지 않게 한다."""


def refuse_in_production(environ=None) -> None:
    env = (os.environ if environ is None else environ).get("AI_ENVIRONMENT", "local").strip().lower()
    if env in BLOCKED_ENVIRONMENTS:
        raise StubRefused("PROFILING_TAGGER_STUB 는 성능 측정 전용 — AI_ENVIRONMENT=production 에서는 켤 수 없음")


def from_settings(settings, environ=None) -> StubTransport:
    refuse_in_production(environ)
    return StubTransport(StubDelay(settings.TAGGER_STUB_P50_S, settings.TAGGER_STUB_P95_S, settings.TAGGER_STUB_JITTER,
                                   settings.TAGGER_STUB_MAX_S, settings.TAGGER_STUB_SEED))
