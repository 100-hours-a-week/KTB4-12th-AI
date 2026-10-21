"""저장소 어댑터 — ports.ProfileRunStore · ports.RecipientProfileStore 의 PostgreSQL 구현.

  DbProfileRunStore        ai_profile.profile_runs        실행 기록 — (수신자, source_version) 한 쌍마다 한 행
  DbRecipientProfileStore  ai_profile.recipient_profiles  프로필 — 수신자당 한 행, 최신 분석이 덮어씀
                           + ai_profile.recipient_reviews 리뷰 자식 행 — 프로필과 **한 문장**으로 함께 저장

표 구조·UPSERT 규칙·상태 전이는 docs/DB_전환_설명.md, 근거는 3단계 구현 상세 §10.2·§16.4 및 담당파트 설계서 §1.7.
다른 구현을 끼울 자리는 ports.py(Protocol)이고, 조립은 main.py 한 곳에서만 한다.

커넥션 규칙 (09-28 DB 왕복 줄이기):
  - run_lock 이 잡은 커넥션(AUTOCOMMIT)은 잠금이 풀릴 때까지 들고 있고, **같은 스레드**의 저장소 호출(save·get_run·upsert…)은
    thread-local 로 그 커넥션을 재사용한다 — 문장마다 풀에서 꺼내던 핑·BEGIN·COMMIT 이 없어져 문장 하나 = 왕복 하나.
  - 잠금 밖에서는 풀에서 꺼낸다: 쓰기는 `engine.begin()` 트랜잭션 하나, 읽기는 AUTOCOMMIT(BEGIN/ROLLBACK 없음).
  - 잠금 커넥션에서 도는 문장은 각각 자기 트랜잭션이다(문장 단위 원자성). 결과 저장과 프로필 upsert 가 한 트랜잭션은 아니다 —
    upsert 실패 시 pipeline 이 실행 기록을 FAILED 로 되돌리는 보상이 그 역할을 한다(DB_전환_설명 §6).
  - 프로필 + 리뷰 두 표는 **CTE 한 문장**(_PROFILE_SAVE)으로 쓴다(10-11) — 잠금 커넥션(AUTOCOMMIT)에서도 한 문장이라 원자적이고
    왕복 1번, 풀에서 커넥션을 더 꺼내지 않는다(슬롯당 커넥션 1개 유지). 트랜잭션 세 문장(BEGIN·3·COMMIT)이면 왕복 5번 + 커넥션 1개 더.
    그 문장 앞뒤로 수신자 잠금을 건다(10-13, _PROFILE_LOCK) — 같은 수신자의 두 번호 저장이 겹쳐 지운 리뷰가 남지 않게. 왕복 1 → 3.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from profiling.backend import callback_body
from profiling.types import (
    CategoryScore,
    DislikedCategory,
    ErrorCode,
    ProfileOutcome,
    ProfileTag,
    RecipientProfile,
    ReviewRecord,
    RunStatus,
    SearchResult,
)

log = logging.getLogger(__name__)

_lock_local = threading.local()      # run_lock 이 잡은 커넥션 — 같은 스레드의 저장소 호출이 재사용한다


def _lock_conn() -> sa.Connection | None:
    """이 스레드가 run_lock 안이면 그 커넥션, 아니면 None."""
    return getattr(_lock_local, "conn", None)


@contextmanager
def _autocommit(engine: Engine) -> Iterator[sa.Connection]:
    """읽기 한 문장용 커넥션 — 트랜잭션을 열지 않아 BEGIN/ROLLBACK 왕복이 없다."""
    conn = engine.connect()
    try:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        yield conn
    finally:
        conn.close()


def _one(engine: Engine, stmt: Any, params: dict[str, Any], fetch: Any, *, write: bool) -> Any:
    """문장 하나를 실행하고 fetch(result) 를 돌려준다. 잠금 커넥션을 쥔 스레드면 거기서(왕복 1), 아니면 풀에서 꺼내
    쓰기는 트랜잭션 하나(engine.begin), 읽기는 AUTOCOMMIT 으로."""
    conn = _lock_conn()
    if conn is not None:
        return fetch(conn.execute(stmt, params))
    if write:
        with engine.begin() as c:
            return fetch(c.execute(stmt, params))
    with _autocommit(engine) as c:
        return fetch(c.execute(stmt, params))


# ===========================================================================
# ai_profile.profile_runs — 실행 기록
# ===========================================================================

RUNS_TABLE = "ai_profile.profile_runs"

_RUN_UPSERT = sa.text(f"""
    insert into {RUNS_TABLE}
        (recipient_user_id, source_version, input_hash, status, catalog_version_id, callback_payload, callback_hash, callback_attempts, error,
         tag_status, prompt_version, validator_version, llm_cost_usd)
    values
        (:rid, :sv, :input_hash, :status, :catalog_version_id, cast(:callback_payload as jsonb), :callback_hash, :callback_attempts, cast(:error as jsonb),
         :tag_status, :prompt_version, :validator_version, :llm_cost_usd)
    on conflict (recipient_user_id, source_version) do update set
        status             = excluded.status,
        input_hash         = excluded.input_hash,
        attempt            = {RUNS_TABLE}.attempt + (case when excluded.status = 'RUNNING' then 1 else 0 end),
        catalog_version_id = coalesce(excluded.catalog_version_id, {RUNS_TABLE}.catalog_version_id),
        callback_payload   = coalesce(excluded.callback_payload, {RUNS_TABLE}.callback_payload),
        callback_hash      = coalesce(excluded.callback_hash, {RUNS_TABLE}.callback_hash),
        callback_attempts  = greatest(excluded.callback_attempts, {RUNS_TABLE}.callback_attempts),
        error              = excluded.error,
        tag_status         = coalesce(excluded.tag_status, {RUNS_TABLE}.tag_status),          -- 콜백 뒤 저장 · 재전송은 이 값을 안 실어 온다
        prompt_version     = coalesce(excluded.prompt_version, {RUNS_TABLE}.prompt_version),
        validator_version  = coalesce(excluded.validator_version, {RUNS_TABLE}.validator_version),
        llm_cost_usd       = coalesce(excluded.llm_cost_usd, {RUNS_TABLE}.llm_cost_usd),
        updated_at         = now()
    returning id, attempt""")

## 더 새 번호가 Backend 에 반영됐을 때(DELIVERED · 409 SUPERSEDED) 그보다 낮은 번호의 미전달(RESULT_READY) 행 — Backend 는 최신 번호만
## 복구 전송하므로 다시 묻지 않는 번호다. 그대로 두면 /health 의 undelivered 가 영영 1 이상으로 고정된다(10-08 실측 S3: v1).
_SUPERSEDE_OLDER = sa.text(f"""
    update {RUNS_TABLE}
       set status = 'SUPERSEDED',
           error = cast(:error as jsonb),
           updated_at = now()
     where recipient_user_id = :rid
       and source_version < :sv
       and status = 'RESULT_READY'""")

_RUN_COLUMNS = ("recipient_user_id, source_version, input_hash, status, catalog_version_id, "
                "callback_payload, callback_attempts, error, updated_at, tag_status, prompt_version, validator_version, llm_cost_usd")

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
        tag_status=row["tag_status"], prompt_version=row["prompt_version"], validator_version=row["validator_version"],
        llm_cost_usd=float(row["llm_cost_usd"]) if row["llm_cost_usd"] is not None else None,
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
            "tag_status": outcome.tag_status, "prompt_version": outcome.prompt_version,
            "validator_version": outcome.validator_version, "llm_cost_usd": outcome.llm_cost_usd,
        }
        run_id, attempt = _one(self._engine, _RUN_UPSERT, params, lambda r: r.one(), write=True)
        log.debug("profile_runs recipient=%s source_version=%s → %s (id=%s attempt=%s callback_attempts=%s)",
                  outcome.recipient_user_id, outcome.source_version, outcome.status, run_id, attempt, outcome.callback_attempts)
        if outcome.status in (RunStatus.DELIVERED, RunStatus.SUPERSEDED):
            # 이 번호가 Backend 에 반영됐다(또는 더 새 것이 이미 있다) → 낮은 번호의 미전달 행은 다시 보낼 일이 없다. 문장 하나, 멱등.
            reason = f"더 새 번호 v{outcome.source_version}가 Backend에 반영됨 — Backend가 다시 묻지 않는 번호"
            superseded = _one(self._engine, _SUPERSEDE_OLDER,
                              {"rid": outcome.recipient_user_id, "sv": outcome.source_version,
                               "error": json.dumps({"code": ErrorCode.CALLBACK_STALE.value, "reason": reason}, ensure_ascii=False)},
                              lambda r: r.rowcount, write=True)
            if superseded:
                log.info("profile_runs recipient=%s: v%s 보다 낮은 미전달 행 %d건을 SUPERSEDED 로 내림",
                         outcome.recipient_user_id, outcome.source_version, superseded)

    def get(self, recipient_user_id: int) -> ProfileOutcome | None:
        row = _one(self._engine, _RUN_LATEST, {"rid": recipient_user_id}, lambda r: r.mappings().first(), write=False)
        return _to_outcome(row)

    def get_run(self, recipient_user_id: int, source_version: int) -> ProfileOutcome | None:
        row = _one(self._engine, _RUN_BY_KEY, {"rid": recipient_user_id, "sv": source_version}, lambda r: r.mappings().first(), write=False)
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
          - 잠금을 쥔 동안 **같은 스레드의 저장소 호출은 이 커넥션을 재사용**한다(thread-local `_lock_local`). 문장마다 풀에서
            꺼내던 핑·BEGIN·COMMIT 이 사라져 문장 하나 = 왕복 하나. 나가면서 반드시 비운다(finally).
        """
        key = _lock_key(recipient_user_id, source_version)
        conn = None
        try:
            conn = self._engine.connect()                                     # pre-ping 은 여기서 끝난다(스스로 autocommit 을 켰다 끈다)
            conn.execution_options(isolation_level="AUTOCOMMIT")              # 첫 execute 전에. 트랜잭션을 열지 않으니 BEGIN/COMMIT 왕복이 없다
            got = bool(conn.execute(sa.text("select pg_try_advisory_lock(:k)"), {"k": key}).scalar_one())
        except Exception as e:  # noqa: BLE001 — 원본 드라이버 오류도 기존대로 처리하며 입력이 실릴 수 있는 traceback은 남기지 않는다.
            log.error("잠금을 걸지 못함 recipient=%s source_version=%s — 잠금 없이 진행: %s", recipient_user_id, source_version, type(e).__name__)
            if conn is not None:
                conn.close()
            yield True
            return

        try:
            if got:
                _lock_local.conn = conn                # 같은 스레드의 저장소 호출이 이 커넥션을 쓴다
            else:
                log.info("이미 처리 중 recipient=%s source_version=%s — 이번 접수는 건너뛴다", recipient_user_id, source_version)
            yield got
        finally:
            _lock_local.conn = None
            if got:
                try:
                    conn.execute(sa.text("select pg_advisory_unlock(:k)"), {"k": key})
                except sa.exc.SQLAlchemyError as e:
                    log.error("잠금 해제 실패 recipient=%s source_version=%s — 이 커넥션을 버린다: %s", recipient_user_id, source_version,
                              type(e).__name__)
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
        같은 번호로 복구 전송할 때 재전송된다. 더 새 번호가 반영돼 다시 묻지 않을 행은 save() 가 SUPERSEDED 로 내려 여기 세지 않는다(10-08).
        """
        return _one(self._engine, sa.text(f"select count(*) from {RUNS_TABLE} where status = 'RESULT_READY'"), {},
                    lambda r: r.scalar_one(), write=False)

    # ---- 편의 (시험·운영) ---------------------------------------------------------

    def delete_recipient(self, recipient_user_id: int) -> int:
        """그 수신자의 실행 기록 전부 삭제 (사용자 데이터 삭제 요청 · 시험 정리). 지운 행 수."""
        with self._engine.begin() as conn:
            return conn.execute(sa.text(f"delete from {RUNS_TABLE} where recipient_user_id = :rid"), {"rid": recipient_user_id}).rowcount

# ===========================================================================
# ai_profile.recipient_profiles — 수신자 프로필
# ===========================================================================

PROFILES_TABLE = "ai_profile.recipient_profiles"
REVIEWS_TABLE = "ai_profile.recipient_reviews"

# 프로필 + 리뷰 스냅샷을 한 문장으로. 덩어리 셋:
#   p  프로필 upsert. 버전 가드(기존 <= 새)에 막히면 RETURNING 이 0행 → 아래 d·u 가 p 를 조건으로 걸어 아무것도 하지 않는다
#      (옛 분석이 새 분석의 리뷰를 덮지 않게). **p 가 첫 덩어리**라 같은 수신자의 다른 버전이 동시에 와도 프로필 행 잠금에서 줄을 선다.
#      다만 줄을 선 뒤에도 d 의 스냅샷은 문장 시작 때 것이다 — 그래서 upsert() 가 이 문장 **앞에** 수신자 잠금을 건다(_PROFILE_LOCK)
#   d  이번에 안 온 상품의 리뷰 삭제. pids 가 빈 배열이면 전부 삭제(<> all('{}') 는 참)
#   u  이번에 온 리뷰 upsert (incoming = 배열 넷을 unnest 한 행들). d 와 u 의 대상은 겹치지 않는다(안 온 상품 / 온 상품) —
#      한 문장에서 같은 행을 두 번 건드리면 PostgreSQL 이 거부하므로 이 성질이 필요하다
# 부모를 이 문장에서 처음 만들어도 자식 FK 는 문장 끝에 검사되어 통과한다. 리뷰 하나라도 CHECK 에 걸리면 문장 전체가 실패한다(원자성).
# 결과: p 의 행 수(1 = 저장, 0 = 가드에 막힘).
_PROFILE_SAVE = sa.text(f"""
    with incoming as (
        select * from unnest(cast(:pids as bigint[]), cast(:ratings as smallint[]), cast(:hashes as text[]), cast(:positions as smallint[]),
                             cast(:texts as text[]))
            as t(product_id, rating, review_hash, position, review_text_clean)
    ),
    p as (
        insert into {PROFILES_TABLE} (recipient_user_id, source_version, preferred_tags, disliked_tags, disliked_categories,
                                      gift_preference_hash, category_scores,
                                      gift_preference_clean, guard_version, tag_status, prompt_version, validator_version,
                                      recommended_product_ids, catalog_version_id)
        values (:rid, :sv, cast(:preferred as jsonb), cast(:disliked as jsonb), cast(:categories as jsonb),
                :pref_hash, cast(:scores as jsonb),
                :pref_clean, :guard_version, :tag_status, :prompt_version, :validator_version,
                cast(:recommended as jsonb), :catalog_version_id)
        on conflict (recipient_user_id) do update set
            source_version       = excluded.source_version,
            preferred_tags       = excluded.preferred_tags,             -- 축 · 출처가 붙은 모양(DR-040)
            disliked_tags        = excluded.disliked_tags,              -- 같은 모양 — 글에서 뽑은 비선호(DR-043, 0010)
            disliked_categories  = excluded.disliked_categories,
            gift_preference_hash = excluded.gift_preference_hash,
            category_scores      = excluded.category_scores,
            gift_preference_clean   = excluded.gift_preference_clean,      -- 정리본 · 상태 · 30개 — 최신 한 벌로 덮는다
            guard_version           = excluded.guard_version,
            tag_status              = excluded.tag_status,
            prompt_version          = excluded.prompt_version,
            validator_version       = excluded.validator_version,
            recommended_product_ids = excluded.recommended_product_ids,
            catalog_version_id      = excluded.catalog_version_id,
            updated_at           = now()
        where {PROFILES_TABLE}.source_version <= excluded.source_version
        returning recipient_user_id
    ),
    d as (
        delete from {REVIEWS_TABLE} r using p
        where r.recipient_user_id = p.recipient_user_id and r.product_id <> all(cast(:pids as bigint[]))
    ),
    u as (
        insert into {REVIEWS_TABLE} (recipient_user_id, product_id, rating, review_hash, position, review_text_clean)
        select p.recipient_user_id, i.product_id, i.rating, i.review_hash, i.position, i.review_text_clean from p cross join incoming i
        on conflict (recipient_user_id, product_id) do update set
            rating      = excluded.rating,
            review_hash = excluded.review_hash,
            position    = excluded.position,
            review_text_clean = excluded.review_text_clean,
            updated_at  = now()
    )
    select count(*) from p""")

# 같은 수신자의 저장끼리 줄 세우기(이슈 2026-10-13_1706). _PROFILE_SAVE 의 d 는 문장 시작 때 스냅샷으로 리뷰를 훑는다 — 앞선 저장이
# 커밋되기 전에 문장이 시작되면 그 저장이 새로 넣은 리뷰 행을 못 보고 남긴다(프로필은 v4 인데 리뷰에 v3 상품). 잠금을 **먼저** 얻고 나서
# 문장을 시작하면 스냅샷이 앞선 저장의 커밋 뒤가 된다. 세션 잠금 · 문장 셋(잠금 · 저장 · 해제) — 트랜잭션을 열지 않아 잠금 커넥션(AUTOCOMMIT)
# 에서 그대로 돈다. 기다리는 시간은 lock_timeout 이 상한(main._connect_db). 키는 run_lock 의 (수신자, 버전) 키와 다른 글자에서 만든다.
_PROFILE_LOCK = sa.text("select pg_advisory_lock(:k)")
_PROFILE_UNLOCK = sa.text("select pg_advisory_unlock(:k)")


def _profile_lock_key(recipient_user_id: int) -> int:
    """수신자 → 저장 잠금 키(부호 있는 64비트). run_lock 의 키(`"<수신자>:<버전>"`)와 겹치지 않게 접두를 붙여 해시한다."""
    digest = hashlib.sha256(f"profile:{recipient_user_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _save_locked(conn: sa.Connection, key: int, params: dict[str, Any]) -> int:
    """잠금 → _PROFILE_SAVE → 해제. 저장이 실패해도(CHECK 등) 해제한다. 해제에 실패한 커넥션은 버린다 — 풀에 돌아가면 그 수신자가 영영 막힌다."""
    conn.execute(_PROFILE_LOCK, {"k": key})
    try:
        return conn.execute(_PROFILE_SAVE, params).scalar_one()      # count(*) 는 늘 한 행
    finally:
        try:
            conn.execute(_PROFILE_UNLOCK, {"k": key})
        except sa.exc.SQLAlchemyError as e:
            log.error("수신자 저장 잠금 해제 실패 — 이 커넥션을 버린다: %s", type(e).__name__)
            conn.invalidate()


def save_params(profile: RecipientProfile) -> dict[str, Any]:
    """RecipientProfile → _PROFILE_SAVE 의 파라미터(배열 다섯 · JSON 문자열). upsert() 와 경합 시험이 같이 쓴다."""
    rv = profile.reviews
    return {
        "rid": profile.recipient_user_id, "sv": profile.source_version,
        "preferred": json.dumps([t.model_dump() for t in profile.preferred_tags], ensure_ascii=False),
        "disliked": json.dumps([t.model_dump() for t in profile.disliked_tags], ensure_ascii=False),
        "categories": json.dumps([c.model_dump() for c in profile.disliked_categories], ensure_ascii=False),
        "pref_hash": profile.gift_preference_hash,
        "scores": json.dumps([c.model_dump() for c in profile.category_scores]),
        "pids": [r.product_id for r in rv], "ratings": [r.rating for r in rv],
        "hashes": [r.review_hash for r in rv], "positions": [r.position for r in rv],
        "texts": [r.text_clean for r in rv],
        "pref_clean": profile.gift_preference_clean, "guard_version": profile.guard_version,
        "tag_status": profile.tag_status, "prompt_version": profile.prompt_version, "validator_version": profile.validator_version,
        "recommended": json.dumps(profile.recommended_product_ids),
        "catalog_version_id": profile.catalog_version_id,
    }


# 프로필 + 리뷰를 한 번에 읽는다(왕복 1). 리뷰는 자리 순서로 json 배열에 담는다 — 없으면 []
_PROFILE_GET = sa.text(f"""
    select p.recipient_user_id, p.source_version, p.preferred_tags, p.disliked_tags, p.disliked_categories,
           p.gift_preference_hash, p.category_scores, p.updated_at, coalesce(r.reviews, '[]'::json) as reviews,
           p.gift_preference_clean, p.guard_version, p.tag_status, p.prompt_version, p.validator_version,
           p.recommended_product_ids, p.catalog_version_id
    from {PROFILES_TABLE} p
    left join lateral (
        select json_agg(json_build_object('product_id', product_id, 'rating', rating, 'review_hash', review_hash, 'position', position,
                                          'text_clean', review_text_clean)
                        order by position) as reviews
        from {REVIEWS_TABLE} where recipient_user_id = p.recipient_user_id
    ) r on true
    where p.recipient_user_id = :rid""")


class DbRecipientProfileStore:
    """ports.RecipientProfileStore 구현 (PostgreSQL)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ---- ports.RecipientProfileStore --------------------------------------------

    def upsert(self, profile: RecipientProfile) -> None:
        """프로필 + 리뷰 스냅샷을 한 문장으로(_PROFILE_SAVE), 그 앞뒤로 수신자 잠금(_save_locked). 버전 가드에 막히면 두 표 모두 그대로 —
        경고 로그만(예외 아님). 리뷰 하나라도 DB 제약에 걸리면 문장 전체가 실패해 예외가 난다 → pipeline 이 실행 기록을 FAILED 로 되돌린다.
        잠금을 lock_timeout 안에 못 얻어도 예외(같은 처리)."""
        rv = profile.reviews
        params = save_params(profile)
        key = _profile_lock_key(profile.recipient_user_id)
        conn = _lock_conn()
        if conn is not None:                                      # 운영 경로 — run_lock 의 잠금 커넥션(AUTOCOMMIT)에서 왕복 3
            written = _save_locked(conn, key, params)
        else:                                                     # 잠금 밖(시험 · 도구) — 풀에서 꺼내 같은 방식으로
            with _autocommit(self._engine) as c:
                written = _save_locked(c, key, params)
        if written == 0:   # 가드에 걸림 = 기존 행이 더 새 버전 (순서 역전). 리뷰도 건드리지 않았다
            log.warning("recipient_profiles recipient=%s source_version=%s 는 기존 행보다 낮아 무시(리뷰 포함)",
                        profile.recipient_user_id, profile.source_version)
        else:
            log.debug("recipient_profiles recipient=%s source_version=%s 저장 (disliked_categories=%d scores=%d reviews=%d)",
                      profile.recipient_user_id, profile.source_version, len(profile.disliked_categories),
                      len(profile.category_scores), len(rv))

    def get(self, recipient_user_id: int) -> RecipientProfile | None:
        row = _one(self._engine, _PROFILE_GET, {"rid": recipient_user_id}, lambda r: r.mappings().first(), write=False)
        if row is None:
            return None
        return RecipientProfile(
            recipient_user_id=row["recipient_user_id"], source_version=row["source_version"],
            preferred_tags=[ProfileTag(**t) for t in row["preferred_tags"]],
            disliked_tags=[ProfileTag(**t) for t in row["disliked_tags"]],
            disliked_categories=[DislikedCategory(**c) for c in row["disliked_categories"]],
            gift_preference_hash=row["gift_preference_hash"],
            category_scores=[CategoryScore(**c) for c in row["category_scores"]],
            reviews=[ReviewRecord(**r) for r in row["reviews"]],
            gift_preference_clean=row["gift_preference_clean"], guard_version=row["guard_version"],
            tag_status=row["tag_status"],
            prompt_version=row["prompt_version"], validator_version=row["validator_version"],
            recommended_product_ids=list(row["recommended_product_ids"]), catalog_version_id=row["catalog_version_id"],
            updated_at=row["updated_at"],
        )

    def delete(self, recipient_user_id: int) -> bool:
        """프로필 삭제 — 리뷰 자식 행은 FK ON DELETE CASCADE 로 함께 사라진다."""
        with self._engine.begin() as conn:
            return conn.execute(sa.text(f"delete from {PROFILES_TABLE} where recipient_user_id = :rid"), {"rid": recipient_user_id}).rowcount > 0
