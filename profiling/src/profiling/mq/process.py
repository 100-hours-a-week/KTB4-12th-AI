"""요청 한 통 처리 — 설계 §4 잠금·중복 판정 순서 + §5 정상 요청 · 처리 실패 순서 (체크리스트 3 · 4 · 5 · 6).

  handle(delivery)
    1. 파싱(messages.parse_request)
       - 식별 불가(요청 ID · 수신자 · 버전을 못 읽음) → 실패 결과를 만들지 않고 실패 큐(DLQ)만 + 경보 로그
       - 식별은 되는데 규격 · 입력 오류 → 실패 결과(INVALID_REQUEST · UNSUPPORTED_SCHEMA_VERSION) + 실패 큐
    2. 접수 기록 upsert(원본 해시) · 식별자 · 변환본 기록 — DB 에 못 쓰면 분석 · ACK 안 하고 소비를 멈춘 채 제한 간격으로 다시
    3. requestId 잠금 → (수신자, 버전) run_lock(strict) — 못 얻으면 '완료'가 아니다: ACK 안 하고 제한 간격으로 대기
       (식별 불가 메시지는 requestId 대신 원본 해시로 잠근다)
    4. 잠금 안에서 decide() — 충돌 · 저장 결과 재사용 · 중단 실행 재개 · 재시도 대기 · 예산 소진 · 새 분석
    5. 분석(기존 pipeline.profile — v3 → v2 → 태그 없는 추천 폴백이 한 번의 논리 시도)
       - 이미 profile_runs 에 같은 입력 결과가 있으면 새로 검색하지 않는다
       - 일시 오류면 RETRY_WAIT + 다음 시각(지수 대기 + 지터)을 DB 에 적고, 메시지를 쥔 채(미ACK) 그 시각까지 기다렸다 다시
       - 예산을 다 쓰면 최종 실패 ANALYSIS_RETRY_EXHAUSTED
       → 결과 바이트(성공 또는 실패)를 처음 한 번 확정해 저장. 저장 직전에 잠금 커넥션이 살아 있는지 확인(끊겼으면 저장 안 함)
    6. 전달: 저장 결과 발행 → Confirm + Return 없음 → result_confirmed
             → (실패면) 원본 바이트를 실패 큐에 복사 발행 → Confirm → dlq_confirmed  (이미 확인됐으면 생략)
             → SETTLED → 원본 ACK
       발행 실패 · 불명 → 기록은 그대로, 새 소비를 멈추고 제한 간격으로 같은 바이트를 다시(분석 예산을 쓰지 않음)

MQ 경로는 '입력 충돌 → 재분석' · '조회 실패 → None' · '잠금 실패 → return' 을 쓰지 않는다(설계 §4 마지막 문단).

통합 런타임(integration.profiler_runtime)이 끼우는 두 연결 지점 — 단독 실행에서는 둘 다 없다(아무것도 안 함):
  record_input(rq, input_hash)  분석 판정(ANALYZE · RESUME) 뒤 · 분석 전에 받은 입력을 기록(챗봇이 읽는 최신 입력).
                                InputConflict 면 REQUEST_CONFLICT, InputInvalid 면 INVALID_REQUEST(옛 HTTP 400 과 같은 판정),
                                그 밖의 예외는 DB 장애와 같게(ACK 안 함 · 소비 멈춤 · 다시)
  admit()                       분석을 감싸는 관문(context manager). 카탈로그 세대 교체 중이면 NoActiveCatalog —
                                설계 §5 처리 실패 1 · 재시도 소유권 표대로 '카탈로그 일시 오류': 시도 예산을 쓰고 지연 재시도

소비 멈춤은 이유("db" · "publish")마다 이 메시지 처리에서 한 번만 걸고, 끝날 때 건 것만 푼다(_Hold) — 다른 작업이 건 멈춤을 풀지 않는다.
받은 연결이 바뀌면(delivery tag 폐기) 기다리던 것을 그만두고 슬롯을 비운다(ABANDONED) — 재배달된 것이 영속 기록으로 이어 간다.
"""
from __future__ import annotations

import logging
import random
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import Protocol
from uuid import UUID

from profiling.mq import results
from profiling.mq import store as st
from profiling.mq.consumer import Delivery
from profiling.mq.messages import (
    INVALID_REQUEST,
    Identity,
    ParsedRequest,
    Rejected,
    Unidentifiable,
    parse_request,
    stored_request,
)
from profiling.mq.metrics import MqStats
from profiling.mq.publisher import (
    REQUEST_FAILED_ROUTING_KEY,
    RESULT_ROUTING_KEY,
    PublishResult,
)
from profiling.ports import NoActiveCatalog, ProfileRunStore
from profiling.types import ErrorCode, ProfileOutcome, ProfileRequest, RunStatus

log = logging.getLogger("profiling.mq.process")

REUSABLE = (RunStatus.RESULT_READY, RunStatus.DELIVERED, RunStatus.SUPERSEDED)   # profile_runs 에 추천 결과가 있는 상태

UNIDENTIFIABLE = "UNIDENTIFIABLE"      # ⚠️ 내부 기록 · 실패 큐 헤더용 코드 — 결과 메시지를 만들지 않으므로 실패 코드 초안 표에 없다
ANALYSIS_FAILED = "ANALYSIS_FAILED"
ANALYSIS_RETRY_EXHAUSTED = "ANALYSIS_RETRY_EXHAUSTED"
REQUEST_CONFLICT = "REQUEST_CONFLICT"


# ---------------------------------------------------------------------------
# 4. 잠금 안의 판정 — 순수 함수(DB 없이 시험)
# ---------------------------------------------------------------------------

class Action(StrEnum):
    USE_STORED = "USE_STORED"          # 이 원본 행에 결과 바이트가 이미 있다 → 그대로 다시 발행(재분석 없음)
    COPY_BINDING = "COPY_BINDING"      # 같은 요청(같은 requestId · 같은 입력)의 다른 원본 → 바인딩 행의 결과 바이트를 그대로
    CONFLICT = "CONFLICT"              # requestId 또는 (수신자, 버전)에 다른 입력의 정상 바인딩 → REQUEST_CONFLICT
    ANALYZE = "ANALYZE"                # 새로 분석
    RESUME = "RESUME"                  # 잠금을 얻었는데 RUNNING — 중단된 실행. 기록된 시도 횟수로 이어서
    RETRY_LATER = "RETRY_LATER"        # RETRY_WAIT 이고 next_attempt_at 이 아직 — 메시지를 쥔 채 그 시각까지 기다린다
    EXHAUSTED = "EXHAUSTED"            # 커밋된 시도 횟수가 예산에 닿음 → 최종 실패 ANALYSIS_RETRY_EXHAUSTED


@dataclass(frozen=True)
class Plan:
    action: Action
    target: UUID | None = None         # 분석 · 결과를 적을 행(같은 요청의 바인딩 행일 수 있다)
    bind: bool = False                 # target(=이 행)을 최초 정상 바인딩으로 만든다
    reason: str = ""


def decide(own: st.DeliveryRow, bindings: list[st.DeliveryRow], *, max_attempts: int, now: datetime) -> Plan:
    """잠금 두 개를 쥔 채 부른다. own = 이 원본의 행(식별자 · input_hash 기록됨), bindings = binding_for() 결과."""
    if own.result_body is not None:
        return Plan(Action.USE_STORED, own.delivery_id, reason="이 원본의 결과 바이트가 이미 있음")

    key = (own.recipient_user_id, own.source_version)
    req_binding: st.DeliveryRow | None = None
    key_binding: st.DeliveryRow | None = None
    for b in bindings:
        same_req = b.request_id == own.request_id
        same_key = (b.recipient_user_id, b.source_version) == key
        if same_req and (not same_key or b.input_hash != own.input_hash):
            return Plan(Action.CONFLICT, own.delivery_id, reason="같은 requestId 에 다른 수신자 · 버전 · 입력의 바인딩")
        if same_key and b.input_hash != own.input_hash:
            return Plan(Action.CONFLICT, own.delivery_id, reason="같은 (수신자, 버전) 에 다른 input_hash 의 바인딩")
        if same_req:
            req_binding = b
        if same_key:
            key_binding = b

    if req_binding is not None and req_binding.delivery_id != own.delivery_id:
        target = req_binding                                  # 같은 요청의 재전송(다른 원본 바이트) — 바인딩 행이 일의 주인
        if target.result_body is not None:
            return Plan(Action.COPY_BINDING, target.delivery_id, reason="같은 요청의 저장 결과 재사용")
        bind = False
    else:
        target = own
        # ⚠️ 해석: requestId 가 다르고 (수신자, 버전) · 입력이 같은 경우 설계에 충돌로 적혀 있지 않다 → 충돌로 보지 않고
        #    이 행은 바인딩 없이 따로 결과를 만든다(profile_runs 의 같은 입력 결과를 재사용하므로 재검색 없음).
        bind = not own.is_binding and key_binding is None

    if target.state == st.RETRY_WAIT and target.next_attempt_at is not None and target.next_attempt_at > now:
        return Plan(Action.RETRY_LATER, target.delivery_id, bind, "재시도 대기 시각 전")
    if target.analysis_attempts >= max_attempts:
        return Plan(Action.EXHAUSTED, target.delivery_id, bind, f"분석 시도 {target.analysis_attempts}/{max_attempts} 소진")
    if target.state == st.RUNNING:
        return Plan(Action.RESUME, target.delivery_id, bind, "잠금이 풀린 RUNNING — 중단된 실행 재개")
    return Plan(Action.ANALYZE, target.delivery_id, bind, "새 분석")


def reusable_ids(run: ProfileOutcome | None, input_hash: str) -> list[int] | None:
    """profile_runs 에 같은 입력의 추천 결과가 있으면 그 번호 목록(순위 순), 없으면 None. 순수 함수."""
    if run is None or run.status not in REUSABLE or run.input_hash != input_hash or run.search is None:
        return None
    return list(run.search.product_ids[: results.MAX_IDS])


def retry_delay(attempts: int, base_s: float, max_s: float, rand: Callable[[], float] = random.random) -> float:
    """n번째(attempts) 실패 뒤 다음 시도까지의 초 — 설계 §7 "유한 횟수 · 지터". 순수 함수.

    지수 대기 d = min(max_s, base_s × 2^(n-1)) 의 절반은 고정, 나머지 절반은 무작위(같은 때 실패한 요청이 한꺼번에 다시 몰리지 않게).
    """
    d = min(max_s, base_s * 2 ** min(max(attempts - 1, 0), 30))
    return d / 2 + rand() * d / 2


# ---------------------------------------------------------------------------
# 5 · 6. 처리 함수
# ---------------------------------------------------------------------------

class Handled(StrEnum):
    ACKED = "ACKED"                        # 결과(·실패 큐) 발행 확인 → SETTLED → ACK
    STOPPED = "STOPPED"                    # 종료 중 — ACK 안 함(재배달되면 영속 기록으로 이어 간다)
    ABANDONED = "ABANDONED"                # 받은 연결이 바뀜 — 이 tag 로는 ACK 불가. 기다림을 그만두고 재배달에 맡김
    ACK_LOST = "ACK_LOST"                  # SETTLED 까지 했는데 ACK 를 못 보냄(연결 바뀜) — 재배달되면 저장 결과를 다시 발행


class Control(Protocol):
    """소비자(RequestConsumer)의 제어 — pause(이유) · resume(이유) · stopping · wait(초) → 종료면 True."""

    stopping: bool

    def pause(self, reason: str) -> None: ...
    def resume(self, reason: str) -> None: ...
    def wait(self, seconds: float) -> bool: ...


class PublisherPort(Protocol):
    def publish(self, routing_key: str, body: bytes, *, message_id: str | None, correlation_id: str | None = None,
                headers: dict | None = None) -> PublishResult: ...


class _DbDown(Exception):
    """AI DB 에 읽거나 쓰지 못했다(잠금 커넥션이 끊긴 것 포함) — 분석 · ACK 하지 않는다. 원래 예외는 __cause__."""


class InputConflict(Exception):
    """record_input 이 '같은 (수신자, 버전)에 이미 다른 입력이 있다'고 알림 → REQUEST_CONFLICT."""


class InputInvalid(Exception):
    """record_input 이 '저장할 수 없는 글(NUL 등)'이라고 알림 → INVALID_REQUEST(다시 해도 같으므로 최종)."""


class _AnalysisDbFailure(Exception):
    """pipeline.profile 이 STORE_FAILED(DB) 로 끝났다 — _DbDown 으로 바뀐다."""


@dataclass(frozen=True)
class _Ready:
    """전달할 준비가 된 행 — 결과 바이트(없을 수 있음) · 실패 큐 필요 여부."""

    delivery_id: UUID
    request_id: str | None             # 결과 message_id · correlation_id. 식별 불가면 None(결과 없음)
    body_sha256: str
    body: bytes | None
    failure_code: str | None
    dlq_required: bool
    dlq_confirmed: bool

    @classmethod
    def of(cls, row: st.DeliveryRow) -> _Ready:
        return cls(row.delivery_id, row.request_id, row.request_body_sha256, row.result_body, row.failure_code, row.dlq_required,
                   row.dlq_confirmed)


@dataclass(frozen=True)
class _RetryAt:
    at: datetime


_WAIT_LOCK = object()


class _Hold:
    """이 메시지 처리가 건 소비 멈춤 — 이유마다 한 번만 걸고, 건 것만 푼다(다른 작업이 건 멈춤을 풀지 않음)."""

    def __init__(self, control: Control) -> None:
        self.control, self.reasons = control, set()

    def pause(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.add(reason)
            self.control.pause(reason)

    def resume(self, reason: str) -> None:
        if reason in self.reasons:
            self.reasons.discard(reason)
            self.control.resume(reason)

    def release(self) -> None:
        for r in sorted(self.reasons):
            self.resume(r)


class Processor:
    def __init__(self, *, deliveries: st.DbMqDeliveryStore, runs: ProfileRunStore, analyze: Callable[[ProfileRequest], ProfileOutcome],
                 publisher: PublisherPort, control: Control, max_attempts: int, wait_s: float, retry_base_s: float, retry_max_s: float,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC), rand: Callable[[], float] = random.random,
                 stats: MqStats | None = None,
                 record_input: Callable[[ProfileRequest, str], None] | None = None,
                 admit: Callable[[], AbstractContextManager] | None = None) -> None:
        self.deliveries, self.runs, self.analyze, self.publisher, self.control = deliveries, runs, analyze, publisher, control
        self.record_input = record_input
        self.admit = admit or nullcontext
        self.max_attempts, self.wait_s, self.retry_base_s, self.retry_max_s = max_attempts, wait_s, retry_base_s, retry_max_s
        self.now, self.rand = now, rand
        self.stats = stats or MqStats()

    # ---- 입구(RequestConsumer 의 handler)
    def handle(self, d: Delivery) -> Handled:
        hold = _Hold(self.control)
        try:
            out = self._handle(d, hold)
        finally:
            hold.release()
        self.stats.inc(f"handled.{out.value.lower()}")
        return out

    def _handle(self, d: Delivery, hold: _Hold) -> Handled:
        prepare: Callable[[], object]
        try:
            p = parse_request(d.body, d.message_id)
        except Unidentifiable as e:
            # 경보: 요청 ID · 수신자 · 버전을 읽을 수 없다 — 어떤 BE 작업도 FAILED 로 만들 수 없으니 운영자가 실패 큐에서 확인
            log.error("mq 경보 식별 불가 요청 sha=%s… message_id_present=%s: %s — 결과 없이 실패 큐로", e.body_sha256[:12],
                      d.message_id is not None, e.reason)
            self.stats.inc("rejected.unidentifiable")
            prepare, rid = partial(self._prepare_unidentifiable, e), None
        except Rejected as e:
            log.warning("mq 요청 거부 requestId=%s %s: %s — 실패 결과 + 실패 큐", e.identity.request_id, e.code, e.reason)
            self.stats.inc(f"rejected.{e.code.lower()}")
            prepare, rid = partial(self._prepare_rejected, e), e.identity.request_id
        else:
            prepare, rid = partial(self._settle_result, p), p.identity.request_id

        r = self._until_ready(d, prepare, rid, hold)
        if isinstance(r, Handled):
            return r
        return self._deliver(d, r, hold)

    # ---- 2 ~ 5. 준비될 때까지 — DB 장애 · 잠금 대기 · 재시도 대기는 메시지를 쥔 채(미ACK) 기다렸다 다시
    def _until_ready(self, d: Delivery, prepare: Callable[[], object], rid: str | None, hold: _Hold) -> _Ready | Handled:
        while True:
            if self.control.stopping:
                return Handled.STOPPED
            if d.stale:
                return Handled.ABANDONED
            try:
                r = prepare()
            except _DbDown as e:
                log.error("mq AI DB 장애 requestId=%s: %s — 소비 일시 중지, %.1f초 뒤 다시", rid, type(e.__cause__).__name__, self.wait_s)
                self.stats.inc("db_down")
                hold.pause("db")
                if self.control.wait(self.wait_s):
                    return Handled.STOPPED
                continue
            hold.resume("db")
            if r is _WAIT_LOCK:
                log.info("mq 잠금 대기 requestId=%s — 다른 실행이 처리 중, ACK 안 함 · %.1f초 뒤 다시", rid, self.wait_s)
                self.stats.inc("lock_wait")
                if self.control.wait(self.wait_s):
                    return Handled.STOPPED
                continue
            if isinstance(r, _RetryAt):
                delay = max((r.at - self.now()).total_seconds(), 0.0)
                log.info("mq 재시도 대기 requestId=%s — %.1f초 뒤 다시 분석(ACK 안 함, 메시지는 prefetch 안에 둔다)", rid, delay)
                if self.control.wait(delay):
                    return Handled.STOPPED
                continue
            return r

    # ---- 1. 식별 불가 — 결과 없이 실패 큐만
    def _prepare_unidentifiable(self, e: Unidentifiable):
        try:
            row = self.deliveries.receive(e.body_sha256)
            with self.deliveries.body_lock(e.body_sha256) as got:
                if not got:
                    return _WAIT_LOCK
                row = self.deliveries.get(row.delivery_id)
                if not row.dlq_required:
                    self.deliveries.mark_dlq_required(row.delivery_id, UNIDENTIFIABLE)
                    row = self.deliveries.get(row.delivery_id)
                return _Ready.of(row)
        except Exception as db:
            raise _DbDown from db

    # ---- 1. 식별은 되는데 거부 — 실패 결과 + 실패 큐
    def _prepare_rejected(self, e: Rejected):
        idn = e.identity
        try:
            row = self.deliveries.receive(e.body_sha256)
            self.deliveries.set_identity(row.delivery_id, request_id=idn.request_id, recipient_user_id=idn.recipient_user_id,
                                         source_version=idn.source_version, input_hash=None, converted=None)
            with self.deliveries.request_lock(idn.request_id) as got:
                if not got:
                    return _WAIT_LOCK
                return self._final(idn, row.delivery_id, row.delivery_id, e.code, e.reason)
        except Exception as db:
            raise _DbDown from db

    # ---- 2 ~ 5. 정상 요청: 접수 · 잠금 · 판정 · 분석 · 결과 저장 → _Ready | _RetryAt | _WAIT_LOCK
    def _settle_result(self, p: ParsedRequest):
        idn = p.identity
        try:
            row = self.deliveries.receive(p.body_sha256)
            self.deliveries.set_identity(row.delivery_id, request_id=idn.request_id, recipient_user_id=idn.recipient_user_id,
                                         source_version=idn.source_version, input_hash=p.input_hash, converted=stored_request(p))
            with self.deliveries.request_lock(idn.request_id) as got_req:
                if not got_req:
                    return _WAIT_LOCK
                with self.runs.run_lock(idn.recipient_user_id, idn.source_version, strict=True) as got_run:
                    if not got_run:
                        return _WAIT_LOCK
                    return self._locked(p, row.delivery_id)
        except _AnalysisDbFailure as e:
            raise _DbDown from e.__cause__
        except Exception as e:
            raise _DbDown from e

    def _locked(self, p: ParsedRequest, own_id: UUID):
        idn = p.identity
        own = self.deliveries.get(own_id)
        plan = decide(own, self.deliveries.binding_for(idn.request_id, idn.recipient_user_id, idn.source_version),
                      max_attempts=self.max_attempts, now=self.now())
        log.info("mq requestId=%s recipient=%s source_version=%s → %s (%s)", idn.request_id, idn.recipient_user_id, idn.source_version,
                 plan.action, plan.reason)
        self.stats.inc(f"decide.{plan.action.value.lower()}")
        if plan.action is Action.USE_STORED:
            return _Ready.of(own)
        if plan.action is Action.COPY_BINDING:
            src = self.deliveries.get(plan.target)
            self.deliveries.set_result(own_id, src.result_body, failure_code=src.failure_code, dlq_required=src.dlq_required)
            return _Ready.of(self.deliveries.get(own_id))
        if plan.action is Action.CONFLICT:
            return self._final(idn, own_id, own_id, REQUEST_CONFLICT, plan.reason)
        if plan.action is Action.RETRY_LATER:
            return _RetryAt(self.deliveries.get(plan.target).next_attempt_at)
        if plan.action is Action.EXHAUSTED:
            return self._final(idn, plan.target, own_id, ANALYSIS_RETRY_EXHAUSTED, plan.reason)

        # ANALYZE · RESUME
        if self.record_input is not None:
            try:
                self.record_input(p.request, p.input_hash)
            except InputConflict as e:
                return self._final(idn, own_id, own_id, REQUEST_CONFLICT, f"입력 기록과 충돌: {e}")
            except InputInvalid as e:
                return self._final(idn, own_id, own_id, INVALID_REQUEST, f"입력 기록 불가: {e}")
        if plan.bind:
            self.deliveries.bind(plan.target)
        try:
            gate = self.admit()
            gate.__enter__()
        except NoActiveCatalog:                                               # 카탈로그 일시 오류(설계 §5) — 예산 안 지연 재시도
            self.stats.inc("admit_closed")
            attempts = self.deliveries.start_attempt(plan.target)
            log.info("mq requestId=%s 분석 관문 닫힘(카탈로그 교체 중) — 시도 %d/%d", idn.request_id, attempts, self.max_attempts)
            return self._transient(idn, plan.target, own_id, attempts, "NO_ACTIVE_CATALOG")
        try:
            return self._analyze_or_reuse(p, plan, own_id)
        finally:
            gate.__exit__(None, None, None)

    def _analyze_or_reuse(self, p: ParsedRequest, plan: Plan, own_id: UUID):
        idn = p.identity
        ids = reusable_ids(self.runs.get_run(idn.recipient_user_id, idn.source_version), p.input_hash)
        if ids is None:
            attempts = self.deliveries.start_attempt(plan.target)          # 분석 시작을 먼저 커밋
            log.info("mq requestId=%s 분석 시도 %d/%d", idn.request_id, attempts, self.max_attempts)
            got = self._analyze(p)
            if isinstance(got, str):
                # 설계 §5 처리 실패 1: 모델 · 카탈로그 일시 오류는 예산 안 지연 재시도. 모델 오류는 pipeline 이 폴백
                # (v3 → v2 → 태그 없음)으로 흡수해 SUCCEEDED(CO-01) — 여기 오는 것은 카탈로그 없음 · 세대 바뀜 · 그 밖의 처리 오류
                return self._transient(idn, plan.target, own_id, attempts, got)
            ids = got
        else:
            self.stats.inc("analysis.reused_profile_run")
            log.info("mq requestId=%s profile_runs 의 같은 입력 결과 재사용(재검색 없음)", idn.request_id)
        try:
            body = results.success_body(idn.request_id, idn.recipient_user_id, idn.source_version, ids, now=self.now())
        except ValueError as e:                                           # 추천 번호가 규격(양의 정수 · 중복 없음 · 30개)을 어김 — 다시 해도 같다
            return self._final(idn, plan.target, own_id, ANALYSIS_FAILED, str(e))
        return self._store(plan.target, own_id, body)

    def _transient(self, idn: Identity, target: UUID, own_id: UUID, attempts: int, reason: str):
        """일시 실패 — 예산이 남았으면 RETRY_WAIT(다음 시각 DB 저장), 다 썼으면 ANALYSIS_RETRY_EXHAUSTED."""
        if attempts >= self.max_attempts:
            return self._final(idn, target, own_id, ANALYSIS_RETRY_EXHAUSTED, f"{reason} — 시도 {attempts}/{self.max_attempts}")
        at = self.now() + timedelta(seconds=retry_delay(attempts, self.retry_base_s, self.retry_max_s, self.rand))
        self.deliveries.set_retry_wait(target, at)
        self.stats.inc("analysis.retry_wait")
        log.warning("mq requestId=%s 분석 일시 실패(%s) — 시도 %d/%d, %s 에 다시", idn.request_id, reason, attempts,
                    self.max_attempts, at.isoformat())
        return _RetryAt(at)

    def _analyze(self, p: ParsedRequest) -> list[int] | str:
        """분석 한 번(폴백 포함). 성공이면 추천 번호, 일시 실패면 사유 문자열. DB 저장 실패는 _AnalysisDbFailure."""
        try:
            outcome = self.analyze(p.request)
        except Exception as e:  # noqa: BLE001 — pipeline.profile 은 예외를 내지 않지만, 나면 일시 실패로(DB 장애로 위장하지 않음)
            self.stats.inc("analysis.failed")
            return type(e).__name__
        if outcome.status is not RunStatus.RESULT_READY:
            if outcome.failure_code is ErrorCode.STORE_FAILED:            # DB 장애를 분석 실패로 위장하지 않는다
                raise _AnalysisDbFailure from RuntimeError("STORE_FAILED")
            self.stats.inc("analysis.failed")
            return f"{outcome.failure_code}"
        self.stats.inc("analysis.succeeded")
        return list(outcome.search.product_ids[: results.MAX_IDS]) if outcome.search else []

    def _final(self, idn: Identity, target: UUID, own_id: UUID, code: str, reason: str) -> _Ready:
        """최종 실패 — 실패 결과 바이트 + dlq_required 를 처음 한 번 저장(설계 §5 처리 실패 2)."""
        log.warning("mq 최종 실패 requestId=%s recipient=%s source_version=%s code=%s (%s)", idn.request_id, idn.recipient_user_id,
                    idn.source_version, code, reason)
        self.stats.inc(f"final_failure.{code.lower()}")
        body = results.failure_body(idn.request_id, idn.recipient_user_id, idn.source_version, code, now=self.now())
        return self._store(target, own_id, body, failure_code=code, dlq_required=True)

    def _store(self, target: UUID, own_id: UUID, body: bytes, *, failure_code: str | None = None, dlq_required: bool = False) -> _Ready:
        # 세션 잠금 커넥션이 끊겼으면 저장하지 않는다(설계 §4) — 다른 실행이 잠금을 얻어 같은 일을 하고 있을 수 있다
        self.deliveries.check_request_lock()
        stored = self.deliveries.set_result(target, body, failure_code=failure_code, dlq_required=dlq_required)
        if target != own_id:
            t = self.deliveries.get(target)
            self.deliveries.set_result(own_id, stored, failure_code=t.failure_code, dlq_required=t.dlq_required)
        return _Ready.of(self.deliveries.get(own_id))

    # ---- 6. 발행 → 확인 기록 → (실패 큐 복사 → 확인 기록) → SETTLED → ACK
    def _deliver(self, d: Delivery, r: _Ready, hold: _Hold) -> Handled:
        rid = r.request_id
        if r.body is not None:
            stop = self._until_published(d, RESULT_ROUTING_KEY, r.body, hold, "result", message_id=rid, correlation_id=rid)
            if stop:
                return stop
            stop = self._until_recorded(d, partial(self.deliveries.mark_result_confirmed, r.delivery_id), hold, rid)
            if stop:
                return stop
        if r.dlq_required and not r.dlq_confirmed:
            # 원본 바이트를 그대로 복사(본문 수정 없음). message_id 는 원본 속성 그대로 — 손상 값을 정상 requestId 로 바꾸지 않는다.
            # 큐 이름은 BE-08 확정. 직접 복사 → Confirm → 원본 ACK 순서는 설계 B 의 참고안이며 이동 방식은 아직 공동 조율(CO-02).
            # ⚠️ 헤더 이름은 설계에 정해져 있지 않다(넣을 값 목록만) — 아래 이름은 이 구현의 선택. 예외 문자열 · 사용자 본문은 넣지 않는다.
            headers = {"delivery_id": str(r.delivery_id), "failure_code": r.failure_code or UNIDENTIFIABLE,
                       "request_body_sha256": r.body_sha256}
            if d.message_id is not None:
                headers["original_message_id"] = d.message_id
            stop = self._until_published(d, REQUEST_FAILED_ROUTING_KEY, d.body, hold, "dlq", message_id=d.message_id, headers=headers)
            if stop:
                return stop
            stop = self._until_recorded(d, partial(self.deliveries.mark_dlq_confirmed, r.delivery_id), hold, rid)
            if stop:
                return stop
        stop = self._until_recorded(d, partial(self.deliveries.settle, r.delivery_id), hold, rid)
        if stop:
            return stop
        hold.release()
        if not d.ack():
            log.warning("mq requestId=%s SETTLED 뒤 ACK 실패(연결 바뀜) — 재배달되면 저장 결과를 다시 발행", rid)
            return Handled.ACK_LOST
        log.info("mq requestId=%s → SETTLED · ACK%s", rid, f" (실패 {r.failure_code})" if r.failure_code else "")
        return Handled.ACKED

    def _until_published(self, d: Delivery, routing_key: str, body: bytes, hold: _Hold, kind: str, *, message_id: str | None,
                         correlation_id: str | None = None, headers: dict | None = None) -> Handled | None:
        """확인(CONFIRMED)될 때까지 같은 바이트를 다시 발행. 확인되면 None, 종료 · 연결 바뀜이면 그 Handled."""
        while True:
            res = self.publisher.publish(routing_key, body, message_id=message_id, correlation_id=correlation_id, headers=headers)
            self.stats.inc(f"publish.{kind}.{res.value.lower()}")
            if res is PublishResult.CONFIRMED:
                hold.resume("publish")
                return None
            log.error("mq %s 발행 %s message_id=%s — 기록 유지, 소비 일시 중지, %.1f초 뒤 같은 바이트 재발행", kind, res, message_id, self.wait_s)
            hold.pause("publish")
            if self.control.wait(self.wait_s):
                return Handled.STOPPED
            if d.stale:
                return Handled.ABANDONED

    def _until_recorded(self, d: Delivery, write: Callable[[], None], hold: _Hold, rid: str | None) -> Handled | None:
        """발행은 됐다 — 기록이 안 되면 기록만 다시(발행을 되풀이하지 않음)."""
        while True:
            try:
                write()
                hold.resume("db")
                return None
            except Exception as e:  # noqa: BLE001
                log.error("mq 전달 기록 실패 requestId=%s: %s — 소비 일시 중지, %.1f초 뒤 다시", rid, type(e).__name__, self.wait_s)
                self.stats.inc("db_down")
                hold.pause("db")
                if self.control.wait(self.wait_s):
                    return Handled.STOPPED
                if d.stale:
                    return Handled.ABANDONED
