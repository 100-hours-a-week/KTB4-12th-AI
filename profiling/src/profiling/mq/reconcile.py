"""대조 복구 — 오래 끝나지 않은 MQ 전달 기록에서 빠진 단계만 마저 한다 (설계 C · CO-06 · CO-02).

설계(11-08 수정본) CO-06: "결과 지연만으로 BE가 동일 요청을 자동 재발행하거나 작업을 FAILED로 전환하지 않는다. … 장기 미완료는 방치하지
않고 저장 작업·AI 결과·전달 상태를 대조해 누락된 단계만 복구하는 별도 절차로 처리한다." CO-02: "발행 실패 시 저장 결과 재발행.
최종 실패 통지·원본 DLQ 보관 중 일부만 성공해도 나머지 복구."

평소에는 메시지를 쥔 작업(process.Processor)이 끝까지 하고, 연결이 끊기면 브로커 재배달이 이어 간다. 이 절차는 그 둘로도 끝나지 않은 기록
(예: 원본 메시지가 큐에서 사라졌는데 결과 발행 확인이 없는 행)을 주기적으로 찾아 대조한다.

  오래된 미완료 행(store.stale_open — 마지막 갱신 · 다음 재시도 시각이 둘 다 MQ_RECONCILE_AFTER_S 보다 오래됨)마다,
  그 행의 잠금(requestId, 식별 불가면 원본 해시)을 얻은 뒤 다시 읽어서:

  | 상태 | 하는 일 | 종류 |
  |---|---|---|
  | 결과 바이트 있음 · 발행 확인 없음 | 같은 바이트를 다시 발행 → 확인되면 result_confirmed | REPUBLISHED (안 되면 PUBLISH_FAILED, 다음 차례에 다시) |
  | 확인은 다 됐는데 SETTLED 만 빠짐 | SETTLED 기록 | SETTLED |
  | 실패 큐 복사 확인 없음 | 하지 않음 — 원본 바이트를 보관하지 않으므로(11-08 결정: 변환본 저장) AI 가 원본을 다시 만들 수 없다 | DLQ_MANUAL (경보 · 운영자가 BE 저장 요청과 대조) |
  | 결과 없음(접수 · 분석 중 · 재시도 대기 후 멈춤) | 재분석하지 않음 — 모델 비용 · 이미 재배달 대기일 수 있음 · 재실행은 BE 와 조율(CO-05) | NO_RESULT (대조 목록) |
  | 잠금을 다른 실행이 쥠 | 건드리지 않음(지금 처리 중) | BUSY |

- 원본 메시지가 아직 큐에 있다가 나중에 오면 처리 함수가 저장 결과를 다시 발행 → SETTLED → ACK 한다(중복 결과는 BE 가 멱등 처리 — BE-05).
- ⚠️ '장기 미완료' 기준 시간 · 실행 간격 · 수동 대조 절차는 설계상 공동 확정 전이다 — 이 모듈은 AI 쪽 제안 구현이며 값은 배포 설정으로 넣는다.
- 로그에는 requestId 만(본문 · 결과 바이트 없음), 지표에는 종류별 개수만 남긴다.

운영자 확인용:  uv run python -m profiling.mq.reconcile --older-than 3600   (읽기만 — 발행 · 기록하지 않는다)
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from profiling.mq import store as st
from profiling.mq.metrics import MqStats
from profiling.mq.publisher import RESULT_ROUTING_KEY, PublishResult

log = logging.getLogger("profiling.mq.reconcile")


class Kind(StrEnum):
    REPUBLISHED = "REPUBLISHED"
    SETTLED = "SETTLED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    DLQ_MANUAL = "DLQ_MANUAL"
    NO_RESULT = "NO_RESULT"
    BUSY = "BUSY"
    ALREADY_SETTLED = "ALREADY_SETTLED"


@dataclass
class Report:
    counts: Counter = field(default_factory=Counter)
    manual: list[tuple[str, str]] = field(default_factory=list)     # (requestId 또는 'sha:앞 12자', 종류) — 사람이 대조할 것

    def summary(self) -> dict:
        return {"counts": dict(sorted(self.counts.items())), "manual": len(self.manual)}


def classify(row: st.DeliveryRow) -> Kind:
    """잠금 없이 행만 보고 무엇이 빠졌는지(읽기 전용 확인 · 복구 판단 공용). 순수 함수."""
    if row.state == st.SETTLED:
        return Kind.ALREADY_SETTLED
    if row.result_body is not None and not row.result_confirmed:
        return Kind.REPUBLISHED
    if row.dlq_required and not row.dlq_confirmed:
        return Kind.DLQ_MANUAL
    if row.result_body is None and not row.dlq_required:
        return Kind.NO_RESULT
    return Kind.SETTLED


def _label(row: st.DeliveryRow) -> str:
    return row.request_id or f"sha:{row.request_body_sha256[:12]}"


class Reconciler:
    def __init__(self, deliveries, publisher, *, older_than_s: float, batch: int = 100, stats: MqStats | None = None) -> None:
        self.deliveries, self.publisher, self.older_than_s, self.batch = deliveries, publisher, older_than_s, batch
        self.stats = stats or MqStats()
        self.last: dict | None = None

    def run_once(self) -> Report:
        rep = Report()
        for row in self.deliveries.stale_open(self.older_than_s, self.batch):
            kind = self._one(row)
            rep.counts[kind.value] += 1
            self.stats.inc(f"reconcile.{kind.value.lower()}")
            if kind in (Kind.DLQ_MANUAL, Kind.NO_RESULT):
                rep.manual.append((_label(row), kind.value))
        for who, kind in rep.manual:
            log.warning("mq 대조 필요 %s %s — %s", who, kind,
                        "실패 큐 복사 미확인(원본 미보관 — BE 저장 요청과 대조)" if kind == Kind.DLQ_MANUAL else
                        "결과 없이 오래 멈춤(재분석 안 함 — BE 작업 상태와 대조, 재실행은 BE 와 조율)")
        if rep.counts:
            log.info("mq 대조 복구 %s", rep.summary())
        self.last = rep.summary()
        return rep

    def _one(self, row: st.DeliveryRow) -> Kind:
        lock = (self.deliveries.request_lock(row.request_id) if row.request_id
                else self.deliveries.body_lock(row.request_body_sha256))
        with lock as got:
            if not got:
                return Kind.BUSY
            row = self.deliveries.get(row.delivery_id)                 # 잠근 뒤 다시 읽는다 — 그사이 처리 함수가 끝냈을 수 있다
            kind = classify(row)
            if kind in (Kind.ALREADY_SETTLED, Kind.DLQ_MANUAL, Kind.NO_RESULT):
                return kind
            if kind is Kind.REPUBLISHED:
                res = self.publisher.publish(RESULT_ROUTING_KEY, row.result_body, message_id=row.request_id,
                                             correlation_id=row.request_id)
                self.stats.inc(f"publish.reconcile.{res.value.lower()}")
                if res is not PublishResult.CONFIRMED:
                    log.error("mq 대조 복구 재발행 %s requestId=%s — 다음 차례에 다시", res, row.request_id)
                    return Kind.PUBLISH_FAILED
                self.deliveries.mark_result_confirmed(row.delivery_id)
                if row.dlq_required and not row.dlq_confirmed:
                    return Kind.DLQ_MANUAL                             # 결과는 마저 보냈다. 실패 큐는 사람이
            self.deliveries.settle(row.delivery_id)
            log.info("mq 대조 복구 %s requestId=%s → SETTLED", kind, row.request_id)
            return kind


# ---------------------------------------------------------------------------
# 운영자 확인(읽기만)
# ---------------------------------------------------------------------------

def report_only(deliveries, older_than_s: float, batch: int) -> list[tuple[str, str, str]]:
    """(대상, 상태, 빠진 것) 목록 — 발행 · 기록하지 않는다."""
    return [(_label(r), r.state, classify(r).value) for r in deliveries.stale_open(older_than_s, batch)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MQ 장기 미완료 전달 기록 확인(읽기만)")
    ap.add_argument("--older-than", type=float, required=True, help="이 초보다 오래 끝나지 않은 기록")
    ap.add_argument("--limit", type=int, default=100)
    a = ap.parse_args(argv)
    import sqlalchemy as sa

    from profiling.settings import get_settings
    engine = sa.create_engine(get_settings().DATABASE_URL, future=True, hide_parameters=True)
    try:
        rows = report_only(st.DbMqDeliveryStore(engine), a.older_than, a.limit)
    finally:
        engine.dispose()
    print("대상\t상태\t빠진 것")
    for r in rows:
        print("\t".join(r))
    print(f"— {len(rows)}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
