"""저장소 어댑터 — ports.ProfileRunStore · ports.RecipientProfileStore 의 PostgreSQL 판과 메모리 판.

  DbProfileRunStore · MemoryProfileRunStore              ai_profile.profile_runs        실행 기록 — (수신자, source_version) 한 쌍마다 한 행
  DbRecipientProfileStore · MemoryRecipientProfileStore  ai_profile.recipient_profiles  프로필 — 수신자당 한 행, 최신 분석이 덮어씀

같은 계약이라 main.py 의 조립 한 줄(PROFILING_STORE=db|memory)만 바꾸면 교체된다.
표 구조·UPSERT 규칙·상태 전이는 docs/DB_전환_설명.md, 근거는 3단계 구현 상세 §10.2·§16.4 및 담당파트 설계서 §1.7.

스레드 안전: DB 판은 Engine 커넥션 풀(메서드마다 `with engine.begin()` 하나), 메모리 판은 Lock 하나.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from profiling.backend import callback_body
from profiling.types import (
    DislikedCategory,
    ErrorCode,
    ProfileOutcome,
    RecipientProfile,
    RunStatus,
    SearchResult,
)

log = logging.getLogger(__name__)



# ===========================================================================
# ai_profile.profile_runs — 실행 기록
# ===========================================================================

log = logging.getLogger(__name__)

RUNS_TABLE = "ai_profile.profile_runs"

_RUN_UPSERT = sa.text(f"""
    insert into {RUNS_TABLE}
        (recipient_user_id, source_version, input_hash, status, catalog_version_id, callback_payload, callback_hash, callback_attempts, error)
    values
        (:rid, :sv, :input_hash, :status, :catalog_version_id, cast(:callback_payload as jsonb), :callback_hash, :callback_attempts, cast(:error as jsonb))
    on conflict (recipient_user_id, source_version) do update set
        status             = excluded.status,
        input_hash         = excluded.input_hash,
        attempt            = {RUNS_TABLE}.attempt + (case when excluded.status = 'RUNNING' then 1 else 0 end),
        catalog_version_id = coalesce(excluded.catalog_version_id, {RUNS_TABLE}.catalog_version_id),
        callback_payload   = coalesce(excluded.callback_payload, {RUNS_TABLE}.callback_payload),
        callback_hash      = coalesce(excluded.callback_hash, {RUNS_TABLE}.callback_hash),
        callback_attempts  = greatest(excluded.callback_attempts, {RUNS_TABLE}.callback_attempts),
        error              = excluded.error,
        updated_at         = now()
    returning id, attempt""")

_RUN_LATEST = sa.text(f"""
    select recipient_user_id, source_version, input_hash, status, catalog_version_id, callback_payload, callback_attempts, error
    from {RUNS_TABLE} where recipient_user_id = :rid
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
            run_id, attempt = conn.execute(_RUN_UPSERT, params).one()
        log.debug("profile_runs recipient=%s source_version=%s → %s (id=%s attempt=%s callback_attempts=%s)",
                  outcome.recipient_user_id, outcome.source_version, outcome.status, run_id, attempt, outcome.callback_attempts)

    def get(self, recipient_user_id: int) -> ProfileOutcome | None:
        with self._engine.begin() as conn:
            row = conn.execute(_RUN_LATEST, {"rid": recipient_user_id}).mappings().first()
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
            return conn.execute(sa.text(f"delete from {RUNS_TABLE} where recipient_user_id = :rid"), {"rid": recipient_user_id}).rowcount

# ===========================================================================
# 실행 기록 (메모리) — DB 없이 돌릴 때
# ===========================================================================


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

# ===========================================================================
# ai_profile.recipient_profiles — 수신자 프로필
# ===========================================================================

PROFILES_TABLE = "ai_profile.recipient_profiles"

_PROFILE_UPSERT = sa.text(f"""
    insert into {PROFILES_TABLE} (recipient_user_id, source_version, preferred_tags, disliked_tags, disliked_categories)
    values (:rid, :sv, cast(:preferred as jsonb), cast(:disliked as jsonb), cast(:categories as jsonb))
    on conflict (recipient_user_id) do update set
        source_version      = excluded.source_version,
        preferred_tags      = excluded.preferred_tags,
        disliked_tags       = excluded.disliked_tags,
        disliked_categories = excluded.disliked_categories,
        updated_at          = now()
    where {PROFILES_TABLE}.source_version <= excluded.source_version""")

_PROFILE_GET = sa.text(f"""
    select recipient_user_id, source_version, preferred_tags, disliked_tags, disliked_categories, updated_at
    from {PROFILES_TABLE} where recipient_user_id = :rid""")


class DbRecipientProfileStore:
    """ports.RecipientProfileStore 구현 (PostgreSQL)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ---- ports.RecipientProfileStore --------------------------------------------

    def upsert(self, profile: RecipientProfile) -> None:
        params = {
            "rid": profile.recipient_user_id, "sv": profile.source_version,
            "preferred": json.dumps(profile.preferred_tags, ensure_ascii=False),
            "disliked": json.dumps(profile.disliked_tags, ensure_ascii=False),
            "categories": json.dumps([c.model_dump() for c in profile.disliked_categories], ensure_ascii=False),
        }
        with self._engine.begin() as conn:
            written = conn.execute(_PROFILE_UPSERT, params).rowcount
        if written == 0:   # WHERE에 걸림 = 기존 행이 더 새 버전 (순서 역전)
            log.warning("recipient_profiles recipient=%s source_version=%s 는 기존 행보다 낮아 무시", profile.recipient_user_id, profile.source_version)
        else:
            log.debug("recipient_profiles recipient=%s source_version=%s 저장 (disliked_categories=%d)",
                      profile.recipient_user_id, profile.source_version, len(profile.disliked_categories))

    def get(self, recipient_user_id: int) -> RecipientProfile | None:
        with self._engine.begin() as conn:
            row = conn.execute(_PROFILE_GET, {"rid": recipient_user_id}).mappings().first()
        if row is None:
            return None
        return RecipientProfile(
            recipient_user_id=row["recipient_user_id"], source_version=row["source_version"],
            preferred_tags=list(row["preferred_tags"]), disliked_tags=list(row["disliked_tags"]),
            disliked_categories=[DislikedCategory(**c) for c in row["disliked_categories"]],
            updated_at=row["updated_at"],
        )

    def delete(self, recipient_user_id: int) -> bool:
        with self._engine.begin() as conn:
            return conn.execute(sa.text(f"delete from {PROFILES_TABLE} where recipient_user_id = :rid"), {"rid": recipient_user_id}).rowcount > 0

# ===========================================================================
# 수신자 프로필 (메모리) — 목, 내용은 채울 것
# ===========================================================================

## 메모리 구현을 위한 임시 클래스 -> DB 연동시 변경예정
class MemoryRecipientProfileStore:
    """ports.RecipientProfileStore 구현 (메모리)."""

    def __init__(self) -> None:
        self._items: dict[int, RecipientProfile] = dict()
        self._lock = threading.Lock()

    def upsert(self, profile: RecipientProfile) -> None:



        raise NotImplementedError

    def get(self, recipient_user_id: int) -> RecipientProfile | None:


        raise NotImplementedError

    def delete(self, recipient_user_id: int) -> bool:
 
        ## 제거할려는 아이템이 없다 -> 에러 발생(항목없음)
        if recipient_user_id in self._items:
            
            return 
        ## 제거할려는 아이템이 있다. -> 제거
        else:
            self._items[recipient_user_id] = []
        raise NotImplementedError
