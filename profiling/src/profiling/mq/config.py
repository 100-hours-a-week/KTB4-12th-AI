"""MQ 설정 확인 (설계 §7 설정표 아래 문단).

"수치 미확정 항목은 AI가 배포 설정으로 입력하고 누락/0/음수/상호 모순이면 활성화를 거부한다.
 라이브러리 기본값에 맡겨 무제한 소비가 되지 않게 한다."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from profiling.settings import Settings

EXCHANGE = "profiling.exchange"            # 설계 §1 브로커 구성 — BE/인프라가 선언, AI 는 선언하지 않는다
REQUEST_QUEUE = "profiling.request"


class MqConfigError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class MqConfig:
    url: str = field(repr=False)          # 계정 · 비밀번호가 들어 있다 — repr · 로그에 찍히지 않게
    prefetch: int
    slots: int
    reconnect_delay_s: float
    shutdown_grace_s: float
    confirm_timeout_s: float
    wait_s: float
    max_analysis_attempts: int
    retry_base_s: float
    retry_max_s: float
    reconcile_after_s: float
    reconcile_interval_s: float


def load(s: Settings) -> MqConfig:
    """켜져 있을 때만 부른다. 문제가 있으면 MqConfigError(문제 목록) — 기동 거부."""
    problems: list[str] = []
    url = s.MQ_URL.get_secret_value()
    scheme = urlparse(url).scheme if url else ""
    if scheme not in ("amqp", "amqps"):
        problems.append("PROFILING_MQ_URL: 비었거나 amqp(s):// 주소가 아님")
    for name in ("MQ_PREFETCH", "MQ_SLOTS", "MQ_RECONNECT_DELAY_S", "MQ_SHUTDOWN_GRACE_S", "MQ_CONFIRM_TIMEOUT_S", "MQ_WAIT_S",
                 "MQ_MAX_ANALYSIS_ATTEMPTS", "MQ_RETRY_BASE_S", "MQ_RETRY_MAX_S",
                 "MQ_RECONCILE_AFTER_S", "MQ_RECONCILE_INTERVAL_S"):
        v = getattr(s, name)
        if v is None:
            problems.append(f"PROFILING_{name}: 없음")
        elif v <= 0:
            problems.append(f"PROFILING_{name}: 0 이하")
    if not problems and s.MQ_PREFETCH < s.MQ_SLOTS:
        problems.append("PROFILING_MQ_PREFETCH < PROFILING_MQ_SLOTS: 받을 수 있는 수보다 슬롯이 많음(슬롯 일부가 영영 놀게 됨)")
    if not problems and s.MQ_RETRY_MAX_S < s.MQ_RETRY_BASE_S:
        problems.append("PROFILING_MQ_RETRY_MAX_S < PROFILING_MQ_RETRY_BASE_S: 최대 대기가 기준 대기보다 짧음")
    if not problems and s.MQ_RECONCILE_AFTER_S <= s.MQ_RETRY_MAX_S:
        problems.append("PROFILING_MQ_RECONCILE_AFTER_S <= PROFILING_MQ_RETRY_MAX_S: 재시도를 기다리는 정상 메시지까지 장기 미완료로 봄")
    if problems:
        raise MqConfigError(problems)
    return MqConfig(url, s.MQ_PREFETCH, s.MQ_SLOTS, s.MQ_RECONNECT_DELAY_S, s.MQ_SHUTDOWN_GRACE_S, s.MQ_CONFIRM_TIMEOUT_S, s.MQ_WAIT_S,
                    s.MQ_MAX_ANALYSIS_ATTEMPTS, s.MQ_RETRY_BASE_S, s.MQ_RETRY_MAX_S,
                    s.MQ_RECONCILE_AFTER_S, s.MQ_RECONCILE_INTERVAL_S)
