"""MQ 처리 지표 — 프로세스 안 누적 숫자(설계 §8 관측 항목 · 체크리스트 8).

설계 §8: "관측 항목은 Ready/unacked·최고 대기 나이·실행 슬롯·중복/충돌·폴백·분석 재시도·결과 재발행·Confirm/Return·
DLQ 미전달·BE 결과 미수신이다. requestId는 로그 추적에 사용하고 개별 ID를 지표 라벨로 사용하지 않는다."

여기엔 이 프로세스가 직접 세는 것만 둔다(이름 = 사건 종류, 라벨에 requestId · 수신자 번호 · 본문 없음).
- 실행 슬롯 · 멈춤 · 진행 중 → consumer.status()
- 최고 대기 나이 · 재시도 대기 · DLQ 미전달 · 결과 미확인 → store.health_counts()(DB, 재시작해도 남음)
- 폴백(v3 → v2 → 태그 없음) → 기존 /health 의 llm 칸(status.LLM)
- Ready · unacked 개수 · BE 결과 미수신 → 브로커 관리 화면 · BE 쪽(이 프로세스가 볼 수 없음)
/health 의 mq 칸이 snapshot() 을 보여 준다. 재시작하면 0 부터 다시 센다.
"""
from __future__ import annotations

import threading
from collections import Counter


class MqStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._c: Counter[str] = Counter()

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._c[name] += n

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self._c.items()))
