"""MQ 전달 기록 저장소 — ai_profile.mq_deliveries (마이그레이션 0012 · 설계 §4 · 체크리스트 3).

설계 §4 "잠금·중복 판정 순서" 의 DB 쪽 부품:
  1. 원본 해시로 접수 기록을 upsert — `receive()`. DB 에 못 쓰면 예외 → 부른 쪽이 분석 · ACK 하지 않고 소비를 멈춘다.
  2. requestId 단위 advisory lock — `request_lock()` (이어서 기존 (수신자, 버전) run_lock(strict=True)). 순서는 늘 이대로.
  3. 최초 정상 바인딩은 잠금 안에서 만들고 바꾸지 않는다 — `bind()`. 바인딩 조회 — `binding_for()`.
  4~5. 저장 결과 재사용 · 중단 실행 재개 — 행의 state · result_body · analysis_attempts 를 읽어 process 가 판단.

모든 메서드는 실패를 삼키지 않는다(조회 실패 → None 으로 바꾸지 않음 — 설계 §4 "기존 _existing_run() 의 조회 실패→None 처리는 MQ 에서 금지").
잠금 커넥션을 쥔 스레드면 그 커넥션으로(stores._one), 아니면 풀에서 꺼낸다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from profiling.stores import _one

log = logging.getLogger("profiling.mq.store")

T = "ai_profile.mq_deliveries"
_req_local = threading.local()      # 이 스레드가 쥔 requestId · 원본 잠금 커넥션(살아 있는지 확인용)
HASH_VERSION = "input-v1"          # pipeline.input_hash 의 규칙 판 — 규칙이 바뀌면 올린다

RECEIVED, RUNNING, RETRY_WAIT, RESULT_READY, SETTLED = "RECEIVED", "RUNNING", "RETRY_WAIT", "RESULT_READY", "SETTLED"


@dataclass(frozen=True)
class DeliveryRow:
    delivery_id: UUID
    request_body_sha256: str
    request_id: str | None
    recipient_user_id: int | None
    source_version: int | None
    input_hash: str | None
    is_binding: bool
    state: str
    analysis_attempts: int
    next_attempt_at: datetime | None
    result_body: bytes | None
    result_confirmed: bool
    dlq_required: bool
    dlq_confirmed: bool
    failure_code: str | None


_COLS = ("delivery_id, request_body_sha256, request_id, recipient_user_id, source_version, input_hash, is_binding, state, "
         "analysis_attempts, next_attempt_at, result_body, result_confirmed, dlq_required, dlq_confirmed, failure_code")


def _row(m: Any) -> DeliveryRow | None:
    if m is None:
        return None
    d = dict(m)
    if d["request_id"] is not None:
        d["request_id"] = str(d["request_id"])
    if d["result_body"] is not None:
        d["result_body"] = bytes(d["result_body"])
    return DeliveryRow(**d)


def request_lock_key(request_id: str) -> int:
    """requestId → advisory lock 키(부호 있는 64비트). run_lock 키(수신자:버전)와 섞이지 않게 접두사를 붙인다."""
    return int.from_bytes(hashlib.sha256(f"mq-request:{request_id}".encode()).digest()[:8], "big", signed=True)


_RECEIVE = sa.text(f"""
    with ins as (
        insert into {T} (request_body_sha256) values (:sha)
        on conflict (request_body_sha256) do nothing
        returning {_COLS})
    select {_COLS} from ins
    union all
    select {_COLS} from {T} where request_body_sha256 = :sha and not exists (select 1 from ins)""")


class DbMqDeliveryStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ---- 1. 접수
    def receive(self, body_sha256: str) -> DeliveryRow:
        """원본 해시로 접수 기록 upsert → 그 행(새로 만들었든 이미 있었든)."""
        return _row(_one(self._engine, _RECEIVE, {"sha": body_sha256}, lambda r: r.mappings().one(), write=True))

    def get(self, delivery_id: UUID) -> DeliveryRow:
        return _row(_one(self._engine, sa.text(f"select {_COLS} from {T} where delivery_id = :id"), {"id": delivery_id},
                         lambda r: r.mappings().one(), write=False))

    def set_identity(self, delivery_id: UUID, *, request_id: str | None, recipient_user_id: int | None, source_version: int | None,
                     input_hash: str | None, converted: dict | None) -> None:
        """파싱한 식별자 · 입력 지문 · 변환본을 적는다(바인딩은 아님). 이미 적힌 값은 바꾸지 않는다(같은 원본이면 같은 값)."""
        _one(self._engine, sa.text(f"""
            update {T} set request_id = coalesce(request_id, cast(:rid as uuid)),
                           recipient_user_id = coalesce(recipient_user_id, :uid), source_version = coalesce(source_version, :sv),
                           input_hash = coalesce(input_hash, :ih), hash_version = coalesce(hash_version, :hv),
                           request_converted = coalesce(request_converted, cast(:conv as jsonb)), updated_at = now()
             where delivery_id = :id"""),
             {"id": delivery_id, "rid": request_id, "uid": recipient_user_id, "sv": source_version, "ih": input_hash,
              "hv": HASH_VERSION if input_hash else None,
              "conv": json.dumps(converted, ensure_ascii=False) if converted is not None else None},
             lambda r: r.rowcount, write=True)

    # ---- 2. 잠금
    @contextmanager
    def request_lock(self, request_id: str) -> Iterator[bool]:
        """requestId 단위 세션 advisory lock(기다리지 않음). 얻으면 True. DB 오류는 그대로 낸다(잠금 없이 진행하지 않음).
        쥔 동안 커넥션을 이 스레드에 기억해 둔다 — `check_request_lock()` 이 결과 저장 직전에 잠금이 살아 있는지 본다."""
        with self._advisory(request_lock_key(request_id), "requestId") as got:
            yield got

    @contextmanager
    def body_lock(self, body_sha256: str) -> Iterator[bool]:
        """원본 해시 단위 잠금 — requestId 를 읽을 수 없는 손상 메시지를 한 곳에서만 처리한다(설계 §4 "원본 해시 기록도 단일 처리")."""
        key = int.from_bytes(hashlib.sha256(f"mq-body:{body_sha256}".encode()).digest()[:8], "big", signed=True)
        with self._advisory(key, "원본") as got:
            yield got

    def check_request_lock(self) -> None:
        """이 스레드가 쥔 requestId 잠금 커넥션이 살아 있는지 — 끊겼으면 예외(세션 잠금이 풀렸다 → 결과를 저장하지 않는다, 설계 §4).
        잠금 밖이면 아무것도 하지 않는다. (수신자, 버전) 잠금은 저장 문장 자체가 그 커넥션으로 가므로 따로 보지 않는다."""
        conn = getattr(_req_local, "conn", None)
        if conn is not None:
            conn.execute(sa.text("select 1")).scalar_one()

    @contextmanager
    def _advisory(self, key: int, what: str) -> Iterator[bool]:
        conn = self._engine.connect()
        try:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            got = bool(conn.execute(sa.text("select pg_try_advisory_lock(:k)"), {"k": key}).scalar_one())
        except Exception:
            conn.close()
            raise
        if got:
            _req_local.conn = conn
        try:
            yield got
        finally:
            if got:
                _req_local.conn = None
                try:
                    conn.execute(sa.text("select pg_advisory_unlock(:k)"), {"k": key})
                except sa.exc.SQLAlchemyError as e:
                    log.error("mq %s 잠금 해제 실패 — 커넥션을 버린다: %s", what, type(e).__name__)
                    conn.invalidate()
            conn.close()

    # ---- 3. 바인딩
    def binding_for(self, request_id: str, recipient_user_id: int, source_version: int) -> list[DeliveryRow]:
        """이 requestId 또는 이 (수신자, 버전) 의 정상 바인딩 행(0 ~ 2개)."""
        return _one(self._engine, sa.text(f"""
            select {_COLS} from {T}
             where is_binding and (request_id = cast(:rid as uuid) or (recipient_user_id = :uid and source_version = :sv))"""),
            {"rid": request_id, "uid": recipient_user_id, "sv": source_version},
            lambda r: [_row(m) for m in r.mappings().all()], write=False)

    def bind(self, delivery_id: UUID) -> None:
        """이 행을 최초 정상 바인딩으로 표시한다(식별자 · input_hash 가 적혀 있어야 함). 한 번 정하면 바꾸지 않는다."""
        _one(self._engine, sa.text(f"update {T} set is_binding = true, updated_at = now() where delivery_id = :id and not is_binding"),
             {"id": delivery_id}, lambda r: r.rowcount, write=True)

    # ---- 4~5. 분석 · 결과 · 전달 상태
    def start_attempt(self, delivery_id: UUID) -> int:
        """분석 시작을 먼저 커밋한다(시도 횟수 +1, RUNNING). 커밋된 횟수를 돌려준다 — 재배달로 초기화하지 않는다."""
        return _one(self._engine, sa.text(f"""
            update {T} set state = 'RUNNING', analysis_attempts = analysis_attempts + 1, next_attempt_at = null, updated_at = now()
             where delivery_id = :id returning analysis_attempts"""), {"id": delivery_id}, lambda r: r.scalar_one(), write=True)

    def set_retry_wait(self, delivery_id: UUID, next_attempt_at: datetime) -> None:
        _one(self._engine, sa.text(f"update {T} set state = 'RETRY_WAIT', next_attempt_at = :at, updated_at = now() where delivery_id = :id"),
             {"id": delivery_id, "at": next_attempt_at}, lambda r: r.rowcount, write=True)

    def set_result(self, delivery_id: UUID, body: bytes, *, failure_code: str | None = None, dlq_required: bool = False) -> bytes:
        """최종 결과 바이트를 처음 한 번 확정한다. 이미 있으면 그대로 두고 **저장된 바이트**를 돌려준다(재발행은 늘 같은 바이트)."""
        return _one(self._engine, sa.text(f"""
            with upd as (
                update {T} set result_body = :body, failure_code = :code, dlq_required = :dlq, state = 'RESULT_READY', updated_at = now()
                 where delivery_id = :id and result_body is null
                returning result_body)
            select result_body from upd
            union all
            select result_body from {T} where delivery_id = :id and not exists (select 1 from upd)"""),
            {"id": delivery_id, "body": body, "code": failure_code, "dlq": dlq_required}, lambda r: bytes(r.scalar_one()), write=True)

    def mark_result_confirmed(self, delivery_id: UUID) -> None:
        _one(self._engine, sa.text(f"update {T} set result_confirmed = true, updated_at = now() where delivery_id = :id"),
             {"id": delivery_id}, lambda r: r.rowcount, write=True)

    def mark_dlq_required(self, delivery_id: UUID, failure_code: str) -> None:
        """결과 없이 실패 큐만 필요한 경우(손상 메시지)."""
        _one(self._engine, sa.text(f"""update {T} set dlq_required = true, failure_code = coalesce(failure_code, :code), updated_at = now()
                                       where delivery_id = :id"""), {"id": delivery_id, "code": failure_code}, lambda r: r.rowcount, write=True)

    def mark_dlq_confirmed(self, delivery_id: UUID) -> None:
        _one(self._engine, sa.text(f"update {T} set dlq_confirmed = true, updated_at = now() where delivery_id = :id"),
             {"id": delivery_id}, lambda r: r.rowcount, write=True)

    def settle(self, delivery_id: UUID) -> None:
        """SETTLED — 결과가 있으면 result_confirmed, dlq_required 면 dlq_confirmed 여야 한다(DB CHECK 가 막는다)."""
        _one(self._engine, sa.text(f"update {T} set state = 'SETTLED', updated_at = now() where delivery_id = :id"),
             {"id": delivery_id}, lambda r: r.rowcount, write=True)

    # ---- 관측(/health) — 설계 §8 관측 항목 중 DB 로 셀 수 있는 것
    def health_counts(self) -> dict:
        """끝나지 않은(SETTLED 아님) 기록 수 · 가장 오래된 것의 나이(초) · 재시도 대기 수 · 실패 큐 미전달 수 · 결과 미확인 수."""
        m = _one(self._engine, sa.text(f"""
            select count(*)                                                            as open,
                   coalesce(extract(epoch from now() - min(created_at)), 0)::float     as oldest_open_s,
                   count(*) filter (where state = 'RETRY_WAIT')                        as retry_wait,
                   count(*) filter (where dlq_required and not dlq_confirmed)          as dlq_pending,
                   count(*) filter (where result_body is not null and not result_confirmed) as result_unconfirmed
              from {T} where state <> 'SETTLED'"""), {}, lambda r: r.mappings().one(), write=False)
        return {k: (round(v, 1) if isinstance(v, float) else int(v)) for k, v in dict(m).items()}

    # ---- 대조 복구(CO-06) — 오래 끝나지 않은 기록
    def stale_open(self, older_than_s: float, limit: int) -> list[DeliveryRow]:
        """SETTLED 가 아니고, 마지막 갱신과 다음 재시도 시각이 모두 older_than_s 보다 오래된 행(오래된 것부터 limit 개).
        재시도 대기 중인 행은 next_attempt_at 이 지나기 전에는 '오래됨'으로 보지 않는다."""
        return _one(self._engine, sa.text(f"""
            select {_COLS} from {T}
             where state <> 'SETTLED'
               and updated_at < now() - make_interval(secs => :s)
               and coalesce(next_attempt_at, updated_at) < now() - make_interval(secs => :s)
             order by created_at limit :n"""), {"s": older_than_s, "n": limit},
            lambda r: [_row(m) for m in r.mappings().all()], write=False)
