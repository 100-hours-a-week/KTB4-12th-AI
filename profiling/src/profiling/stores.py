"""저장소 어댑터 — ports.ProfileRunStore · ports.RecipientProfileStore 의 PostgreSQL 구현.

  DbProfileRunStore        ai_profile.profile_runs        실행 기록 — (수신자, source_version) 한 쌍마다 한 행
  DbRecipientProfileStore  ai_profile.recipient_profiles  프로필 — 수신자당 한 행, 최신 분석이 덮어씀

표 구조·UPSERT 규칙·상태 전이는 docs/DB_전환_설명.md, 근거는 3단계 구현 상세 §10.2·§16.4 및 담당파트 설계서 §1.7.
다른 구현을 끼울 자리는 ports.py(Protocol)이고, 조립은 main.py 한 곳에서만 한다.

스레드 안전: Engine 커넥션 풀이 담당한다 — 메서드마다 `with engine.begin()` 트랜잭션 하나. run_lock 만 예외 — AUTOCOMMIT 커넥션 하나를 잠금이 풀릴 때까지 든다.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
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

_RUN_COLUMNS = ("recipient_user_id, source_version, input_hash, status, catalog_version_id, "
                "callback_payload, callback_attempts, error, updated_at")

_RUN_LATEST = sa.text(f"""
    select {_RUN_COLUMNS}
    from {RUNS_TABLE} where recipient_user_id = :rid
    order by source_version desc, updated_at desc limit 1""")

## 접수 단계 중복 판정용 — 키로 딱 한 행 (Backend가 같은 sourceVersion으로 재전송할 때)
_RUN_BY_KEY = sa.text(f"""
    select {_RUN_COLUMNS}
    from {RUNS_TABLE} where recipient_user_id = :rid and source_version = :sv""")


def _lock_key(recipient_user_id: int, source_version: int) -> int:
    """(수신자, 버전) → advisory lock 키. 두 값을 이어 sha256 한 앞 8바이트를 부호 있는 64비트로.

    두 값을 32비트씩 나눠 담으면 수신자 ID 가 2^32 를 넘는 순간 서로 다른 수신자가 같은 키를 받는다
    (recipient_user_id 는 BIGINT 다). 해시는 어느 쪽이 커져도 그런 가정이 없다.
    """
    digest = hashlib.sha256(f"{recipient_user_id}:{source_version}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def payload_hash(payload: dict[str, Any]) -> str:
    """콜백 본문의 sha256 — 키 순서를 고정해 같은 내용이면 같은 값. 재전송 시 "같은 payload" 확인용(§16.4)."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _to_outcome(row: Any) -> ProfileOutcome | None:
    """profile_runs 한 행 → ProfileOutcome. 없으면 None. get()·get_run()이 같이 쓴다.

    validation(태그)은 복원하지 않는다 — 실행 기록에 저장하지 않기 때문(태그는 recipient_profiles에 있다).
    search는 저장해 둔 콜백 본문에서 되살린다 — 그래야 **재분석 없이 같은 본문으로 재전송**할 수 있다.
    """
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
        updated_at=row["updated_at"],
    )


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
        return _to_outcome(row)

    def get_run(self, recipient_user_id: int, source_version: int) -> ProfileOutcome | None:
        with self._engine.begin() as conn:
            row = conn.execute(_RUN_BY_KEY, {"rid": recipient_user_id, "sv": source_version}).mappings().first()
        return _to_outcome(row)

    @contextmanager
    def run_lock(self, recipient_user_id: int, source_version: int) -> Iterator[bool]:
        """(수신자, 버전) 배타 잠금 — PostgreSQL advisory lock. 얻으면 True, 이미 누가 잡고 있으면 False.

        **세션 단위** 잠금(`pg_try_advisory_lock`)이라 커넥션 하나를 잠금이 풀릴 때까지 들고 있는다. 그래서
        판정부터 콜백까지가 한 잠금 안에 들어가고, 프로세스 밖에서도 통하므로 앱을 여러 개 띄워도 같은 키가 겹쳐
        돌지 않는다. 기다리지 않는다(try) — 이미 누가 처리 중이면 이번 요청은 할 일이 없다.

        세 가지 규칙이 있다 (2026-09-27 이슈 A·C 에서 나온 것):
          - 잠금 커넥션은 **AUTOCOMMIT** 으로 쓴다(`execution_options`, 첫 질의 전에). 트랜잭션을 아예 열지 않으므로 작업 내내
            `idle in transaction` 이 생기지 않고(VACUUM·세션 타임아웃 문제 없음) BEGIN/COMMIT 왕복도 없다 — 잠금·해제가
            각각 문장 하나다. 세션 잠금은 트랜잭션과 무관하다. (09-27 에는 "잠금 직후 커밋"이었다 — 09-28 왕복 줄이기.)
          - 잠금을 **못 걸면 진행한다** — 커넥션이든 질의든. 잠금은 중복을 줄이는 장치이지 접수를 막는 장치가
            아니다(최악이 분석 한 번 더). `execution_options` 의 오류는 SQLAlchemyError 가 아니라 psycopg 원본이라
            `Exception` 으로 받는다.
          - 해제에 실패한 커넥션은 **버린다**(invalidate). 풀에 돌려보내면 세션 잠금이 살아 있어 그 키가 영영 막힌다.
        """
        key = _lock_key(recipient_user_id, source_version)
        conn = None
        try:
            conn = self._engine.connect()                                     # pre-ping 은 여기서 끝난다(스스로 autocommit 을 켰다 끈다)
            conn.execution_options(isolation_level="AUTOCOMMIT")              # 첫 execute 전에. 트랜잭션을 열지 않으니 BEGIN/COMMIT 왕복이 없다
            got = bool(conn.execute(sa.text("select pg_try_advisory_lock(:k)"), {"k": key}).scalar_one())
        except Exception:      # SQLAlchemyError 뿐 아니라 execution_options 가 감싸지 않고 올리는 psycopg 원본 오류까지 — 못 걸면 진행
            log.exception("잠금을 걸지 못함 recipient=%s source_version=%s — 잠금 없이 진행", recipient_user_id, source_version)
            if conn is not None:
                conn.close()
            yield True
            return

        try:
            if not got:
                log.info("이미 처리 중 recipient=%s source_version=%s — 이번 접수는 건너뛴다", recipient_user_id, source_version)
            yield got
        finally:
            if got:
                try:
                    conn.execute(sa.text("select pg_advisory_unlock(:k)"), {"k": key})
                except sa.exc.SQLAlchemyError:
                    log.exception("잠금 해제 실패 recipient=%s source_version=%s — 이 커넥션을 버린다", recipient_user_id, source_version)
                    conn.invalidate()
            conn.close()                                                      # 풀로 돌아가며 격리 수준은 기본값으로 복원된다(왕복 없음)

    # ---- 운영 -------------------------------------------------------------------

    def recover_stale_runs(self, older_than_s: int) -> int:
        """끊긴 실행(오래된 RUNNING)을 FAILED 로 내린다. 바꾼 행 수. 시작할 때 한 번 부른다.

        프로세스가 죽으면 그 행은 RUNNING 인 채 남고, 중복 판정이 "이미 분석 중"으로 읽어 Backend 재전송을
        계속 건너뛴다. **`older_than_s` 보다 오래된 것만** 건드린다 — 다른 인스턴스가 지금 돌리고 있는 행을
        죽이지 않기 위해서다(그래서 중복 판정의 기준과 같은 값을 쓴다).

        FAILED 로 두면 Backend 가 다시 보낼 때 새로 분석한다. AI 는 FAILED 를 콜백하지 않는다(침묵).
        """
        with self._engine.begin() as conn:
            return conn.execute(sa.text(f"""
                update {RUNS_TABLE}
                   set status = 'FAILED',
                       error = cast(:error as jsonb),
                       updated_at = now()
                 where status = 'RUNNING'
                   and updated_at < now() - make_interval(secs => :secs)"""),
                {"secs": older_than_s,
                 "error": json.dumps({"code": ErrorCode.PIPELINE_ERROR.value,
                                      "reason": "프로세스가 끝나 실행 기록만 남음 — 시작 시 정리"}, ensure_ascii=False)},
            ).rowcount

    def undelivered_count(self) -> int:
        """결과는 있는데 Backend 에 전달하지 못한 행 수(RESULT_READY). /health 가 보여준다.

        0 이 정상이다. 늘어나면 7.7 경로(네트워크·Backend 5xx)에 문제가 있다는 뜻이고, 그 행들은 Backend 가
        같은 번호로 다시 보낼 때 재전송된다.
        """
        with self._engine.begin() as conn:
            return conn.execute(sa.text(f"select count(*) from {RUNS_TABLE} where status = 'RESULT_READY'")).scalar_one()

    # ---- 편의 (시험·운영) ---------------------------------------------------------

    def delete_recipient(self, recipient_user_id: int) -> int:
        """그 수신자의 실행 기록 전부 삭제 (사용자 데이터 삭제 요청 · 시험 정리). 지운 행 수."""
        with self._engine.begin() as conn:
            return conn.execute(sa.text(f"delete from {RUNS_TABLE} where recipient_user_id = :rid"), {"rid": recipient_user_id}).rowcount

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
