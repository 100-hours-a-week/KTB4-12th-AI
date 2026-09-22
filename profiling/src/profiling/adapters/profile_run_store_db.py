"""ProfileRunStore (PostgreSQL) — ai_profile.profile_runs. 실행 기록: 접수·결과·전달의 감사 기록 (3단계 구현 상세 §10.2·§16.4).

MemoryProfileRunStore와 같은 계약(ports.ProfileRunStore)이라 main.py의 조립 한 줄만 바꾸면 교체된다.

행 하나 = (recipient_user_id, source_version) 한 쌍 = 실행 한 건. save()는 상태에 따라 같은 행을 갱신한다:

  save(RUNNING)        접수. 없으면 INSERT, 있으면(재실행) status=RUNNING·attempt+1·error 비움
  save(RESULT_READY)   결과. callback_payload(7.7 본문)·callback_hash·catalog_version_id를 **콜백 전에** 커밋 — 재시작·재전송 때 이 값을 그대로 보낸다
  save(DELIVERED | SUPERSEDED | FAILED)   콜백 뒤. status·callback_attempts 갱신, payload는 유지(coalesce)
  save(FAILED, failure_reason)            분석 실패. error={"reason": …}

한 문장(INSERT … ON CONFLICT DO UPDATE)으로 전부 처리한다 — 어느 상태에서 불려도 행이 없으면 만들고 있으면 갱신. 열마다 규칙:
  input_hash        항상 outcome 값 (NOT NULL — pipeline이 매 outcome에 넣는다)
  attempt           RUNNING으로 다시 들어올 때만 +1
  callback_*        새 값이 있으면 덮고 없으면 기존 유지 (coalesce) — DELIVERED 갱신 때 payload가 지워지지 않게
  callback_attempts 큰 쪽 (run_and_callback이 +1 해서 보낸다)
  error             {"code": failure_code, "reason": failure_reason} (RESULT_READY면 NULL로 지워짐 · 미전달이면 CALLBACK_UNREACHABLE 남음)
DB의 CHECK(ck_profile_runs_result_has_payload)가 "결과 없이 결과 상태" 전이를 거부하므로, 어댑터 버그가 있어도 잘못된 행은 남지 않는다.

get(recipient_user_id)는 그 수신자의 **가장 최신 실행**(source_version 큰 것, 같으면 updated_at 늦은 것) 한 건을 ProfileOutcome으로 되돌린다.
validation(태그)은 이 테이블에 없으므로 None — 태그는 recipient_profiles(RecipientProfileStore)에서 읽는다.

스레드 안전: Engine의 커넥션 풀이 담당. 메서드마다 `with engine.begin()` 트랜잭션 하나.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from profiling.adapters.backend_port_http import callback_body
from profiling.profile.types import ErrorCode, ProfileOutcome, RunStatus, SearchResult

log = logging.getLogger(__name__)

TABLE = "ai_profile.profile_runs"

_UPSERT = sa.text(f"""
    insert into {TABLE}
        (recipient_user_id, source_version, input_hash, status, catalog_version_id, callback_payload, callback_hash, callback_attempts, error)
    values
        (:rid, :sv, :input_hash, :status, :catalog_version_id, cast(:callback_payload as jsonb), :callback_hash, :callback_attempts, cast(:error as jsonb))
    on conflict (recipient_user_id, source_version) do update set
        status             = excluded.status,
        input_hash         = excluded.input_hash,
        attempt            = {TABLE}.attempt + (case when excluded.status = 'RUNNING' then 1 else 0 end),
        catalog_version_id = coalesce(excluded.catalog_version_id, {TABLE}.catalog_version_id),
        callback_payload   = coalesce(excluded.callback_payload, {TABLE}.callback_payload),
        callback_hash      = coalesce(excluded.callback_hash, {TABLE}.callback_hash),
        callback_attempts  = greatest(excluded.callback_attempts, {TABLE}.callback_attempts),
        error              = excluded.error,
        updated_at         = now()
    returning id, attempt""")

_LATEST = sa.text(f"""
    select recipient_user_id, source_version, input_hash, status, catalog_version_id, callback_payload, callback_attempts, error
    from {TABLE} where recipient_user_id = :rid
    order by source_version desc, updated_at desc limit 1""")


def payload_hash(payload: dict[str, Any]) -> str:
    """콜백 본문의 sha256 — 키 순서를 고정해 같은 내용이면 같은 값. 재전송 시 "같은 payload" 확인용(§16.4)."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class DbProfileRunStore:
    """ports.ProfileRunStore 구현 (PostgreSQL)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ---- ports.ProfileRunStore --------------------------------------------------

    def save(self, outcome: ProfileOutcome) -> None:
        if outcome.input_hash is None:
            raise ValueError("ProfileOutcome.input_hash가 없다 — pipeline.input_hash(rq)로 채워서 저장해야 한다")

        payload: dict[str, Any] | None = None
        if outcome.search is not None:                                   # RESULT_READY 이후에는 항상 있음
            payload = callback_body(outcome).model_dump()
        error = None
        if outcome.failure_code or outcome.failure_reason:
            error = {"code": outcome.failure_code.value if outcome.failure_code else None, "reason": outcome.failure_reason}

        params = {
            "rid": outcome.recipient_user_id, "sv": outcome.source_version,
            "input_hash": outcome.input_hash, "status": outcome.status.value,
            "catalog_version_id": outcome.search.catalog_version_id if outcome.search else None,
            "callback_payload": json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            "callback_hash": payload_hash(payload) if payload is not None else None,
            "callback_attempts": outcome.callback_attempts,
            "error": json.dumps(error, ensure_ascii=False) if error is not None else None,
        }
        with self._engine.begin() as conn:
            run_id, attempt = conn.execute(_UPSERT, params).one()
        log.debug("profile_runs recipient=%s source_version=%s → %s (id=%s attempt=%s callback_attempts=%s)",
                  outcome.recipient_user_id, outcome.source_version, outcome.status, run_id, attempt, outcome.callback_attempts)

    def get(self, recipient_user_id: int) -> ProfileOutcome | None:
        with self._engine.begin() as conn:
            row = conn.execute(_LATEST, {"rid": recipient_user_id}).mappings().first()
        if row is None:
            return None
        search = None
        if row["callback_payload"] is not None and row["catalog_version_id"] is not None:
            search = SearchResult(product_ids=row["callback_payload"]["recommendedProductIds"], query_text="",
                                  catalog_version_id=row["catalog_version_id"])
        return ProfileOutcome(
            recipient_user_id=row["recipient_user_id"], source_version=row["source_version"],
            status=RunStatus(row["status"]), input_hash=row["input_hash"],
            validation=None, search=search,
            failure_code=ErrorCode(row["error"]["code"]) if (row["error"] or {}).get("code") else None,
            failure_reason=(row["error"] or {}).get("reason"), callback_attempts=row["callback_attempts"],
        )

    # ---- 편의 (시험·운영) ---------------------------------------------------------

    def delete_recipient(self, recipient_user_id: int) -> int:
        """그 수신자의 실행 기록 전부 삭제 (사용자 데이터 삭제 요청 · 시험 정리). 지운 행 수."""
        with self._engine.begin() as conn:
            return conn.execute(sa.text(f"delete from {TABLE} where recipient_user_id = :rid"), {"rid": recipient_user_id}).rowcount
