"""페이크 Backend의 상태 기계 — 모델 API 설계 v3.2.7 §7.6·§7.7 과 BE_연동_필드표 v1 §3, 그리고 BE 계획(09-27) 5-1·5-2 를 옮긴 것.

**판단만 한다.** HTTP도 시간도 여기서 만들지 않는다(`now`를 받는다) — 그래야 DB·서버 없이 시험한다.
실제 Backend가 할 일을 흉내 내는 것이 목적이고, 시험에서는 디바운스·타임아웃·백오프를 초 단위로 줄여 쓴다.

상태가 바뀌는 곳은 넷뿐이다(필드표 §3.3): 202 → PENDING · 7.7 200 → COMPLETED · 타임아웃 소진 → FAILED · 접수 실패 소진 → FAILED.
오류 응답 자체는 상태를 바꾸지 않는다 — 다음 재시도 시각만 적는다.

재시도 모델(BE 계획 5-1·5-2, 09-27):
  - 새 요청은 새 번호(`source_version += 1`, `retry_count = 0`)이고, 그 번호로 실패하면 **같은 번호로** 최대 `max_retry` 회 다시 보낸다.
    접수 실패(500·503·연결 실패·타임아웃)든 결과 미도달(PENDING 타임아웃)이든 **같은 counter** 다.
  - 400·401 은 재시도하지 않는다(계약·토큰 문제 — 고친 뒤 새 번호).
  - 503 은 `Retry-After` 를 우선 쓰고, 없으면 기본 백오프. 500·연결 실패·타임아웃은 기본 백오프.
  - 대기 중인 수정이 있으면(`last_changed_at`) 옛 번호 재시도 대신 새 번호가 나간다 — 낡은 입력으로 재시도하지 않는다.
  - `max_retry` 를 다 쓰고도 실패하면 FAILED. AI 는 그 뒤에도 아무것도 보내지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NONE, PENDING, COMPLETED, FAILED = "NONE", "PENDING", "COMPLETED", "FAILED"
SEND_NEW, SEND_RETRY = "new", "retry"
NO_RETRY_STATUSES = (400, 401)             # BE 5-1 오류표 — 계약·토큰 오류는 재시도하지 않는다


@dataclass(frozen=True)
class Policy:
    """실제 Backend 값은 디바운스 1시간·상한 6시간·타임아웃 10분. 시험에서는 초 단위로 줄인다."""

    debounce_s: float = 5.0            # 마지막 변경 후 이만큼 잠잠하면 보낸다
    window_s: float = 30.0             # 첫 미반영 변경 후 이만큼 지나면 잠잠하지 않아도 보낸다
    pending_timeout_s: float = 10.0    # PENDING이 이보다 오래가면 같은 번호 재시도 또는 FAILED
    max_retry: int = 2                 # 같은 번호로 다시 보내는 최대 횟수 (접수 실패 + 결과 미도달 합산)
    backoff_s: float = 2.0             # 접수 실패 뒤 다음 재시도까지 (503 은 Retry-After 가 우선)


@dataclass
class Recipient:
    """Backend가 수신자마다 들고 있어야 하는 값 (필드표 §3.2 + BE 5-1 재시도 상태)."""

    recipient_user_id: int
    profile_status: str = NONE
    source_version: int = 0                 # 마지막으로 보낸 7.6 번호
    analyzed_source_version: int = 0        # 저장된 7.7 결과의 번호
    recommended_product_ids: list[int] = field(default_factory=list)
    disliked_categories: list[dict] = field(default_factory=list)
    last_changed_at: float | None = None    # 대기 중인 수정 (없으면 None)
    window_started_at: float | None = None
    pending_since: float | None = None
    retry_count: int = 0
    next_retry_at: float | None = None      # 접수 실패 뒤 다음 재시도가 가능한 시각 (BE 5-1)
    last_attempt_at: float | None = None    # 가장 최근 7.6 요청 시각 (BE 5-1)

    def snapshot(self) -> dict:
        return {"recipientUserId": self.recipient_user_id, "profileStatus": self.profile_status,
                "sourceVersion": self.source_version, "analyzedSourceVersion": self.analyzed_source_version,
                "retryCount": self.retry_count, "recommendedCount": len(self.recommended_product_ids),
                "pendingWaiting": self.last_changed_at is not None, "nextRetryAt": self.next_retry_at}


# ---------------------------------------------------------------------------
# 사건 — 사용자가 비선호를 바꿈 (§3.4 ⑥)
# ---------------------------------------------------------------------------


def on_change(r: Recipient, categories: list[dict], now: float) -> None:
    """번호도 상태도 건드리지 않는다. 몇 번을 고치든 다음 7.6 한 번이 그 시점의 최신 상태를 실어 간다."""
    r.disliked_categories = categories
    r.last_changed_at = now
    if r.window_started_at is None:
        r.window_started_at = now


# ---------------------------------------------------------------------------
# 판단 — 지금 7.6 을 보내야 하나 (§3.4 ① · ⑤ · BE 5-2)
# ---------------------------------------------------------------------------


def due_send(r: Recipient, now: float, policy: Policy) -> str | None:
    """SEND_NEW(새 번호) · SEND_RETRY(같은 번호 재시도) · None.

    순서가 곧 우선순위다.
      ① 대기 중인 수정이 있으면 **새 번호만** 본다 — 옛 번호를 재시도하지 않는다(BE 5-2: 신규 변경이 있으면 기존 요청은 만료).
      ② 접수 실패 뒤 잡아 둔 재시도 시각이 왔으면 같은 번호 재시도.
      ③ PENDING 이 타임아웃을 넘겼으면 같은 번호 재시도(결과 미도달).
    """
    if r.last_changed_at is not None:
        quiet = now - r.last_changed_at >= policy.debounce_s
        capped = r.window_started_at is not None and now - r.window_started_at >= policy.window_s
        return SEND_NEW if (quiet or capped) else None
    if r.retry_count < policy.max_retry:
        if r.next_retry_at is not None and now >= r.next_retry_at:
            return SEND_RETRY
        if r.profile_status == PENDING and r.pending_since is not None and now - r.pending_since >= policy.pending_timeout_s:
            return SEND_RETRY
    return None


def start_new(r: Recipient, now: float) -> int:
    """새 번호로 보낼 준비 — 번호를 올려 저장하고(요청보다 먼저), **대기 중인 수정을 여기서 소비한다**(요청 스냅샷).

    재시도 상태는 새 번호마다 0 이다. 소비한 뒤 보내는 도중에 들어온 수정은 `on_accepted` 가 그대로 살려 둔다.
    """
    r.source_version += 1
    r.retry_count = 0
    r.next_retry_at = None
    r.last_attempt_at = now
    r.last_changed_at = None
    r.window_started_at = None
    return r.source_version


def start_retry(r: Recipient, now: float) -> int:
    """같은 번호로 다시 보낸다. 횟수만 올린다 (BE 5-1: 실제 재시도 직전에 증가)."""
    r.retry_count += 1
    r.next_retry_at = None
    r.last_attempt_at = now
    return r.source_version


# ---------------------------------------------------------------------------
# 사건 — 7.6 이 202 로 접수됨 (§3.4 ②)
# ---------------------------------------------------------------------------


def on_accepted(r: Recipient, sent_version: int, changed_at_when_sent: float | None, now: float) -> None:
    """조건 두 개가 핵심이다.

    ① analyzed < source_version 일 때만 PENDING — 7.7이 202보다 먼저 처리되는 경우(v1은 콜백까지 수십 ms)
       COMPLETED를 PENDING으로 덮어 10분 뒤 FAILED로 오판하는 것을 막는다.
    ② 보내기 직전에 읽은 값과 지금 값이 같을 때만 last_changed_at 을 비운다 — 분석 중에 들어온 수정이 사라지지 않게.
    """
    r.next_retry_at = None
    if r.analyzed_source_version < sent_version:
        r.profile_status = PENDING
        r.pending_since = now
    if r.last_changed_at == changed_at_when_sent:
        r.last_changed_at = None
        r.window_started_at = None


# ---------------------------------------------------------------------------
# 사건 — 7.6 이 202 가 아니었다 (BE 5-1 오류표)
# ---------------------------------------------------------------------------


def on_rejected(r: Recipient, status: int | None, retry_after_s: float | None, now: float, policy: Policy) -> bool:
    """접수 실패. 상태는 그대로 두고 **다음 재시도 시각**만 적는다. 재시도가 잡혔으면 True.

    status None 은 연결 실패·응답 타임아웃. 400·401 은 재시도하지 않는다(요청을 고친 뒤 새 번호로).
    `max_retry` 를 이미 다 썼으면 FAILED 로 내린다 — PENDING 타임아웃 소진(`expire`)과 같은 결말이다.
    """
    if status in NO_RETRY_STATUSES:
        r.next_retry_at = None
        return False
    if r.retry_count >= policy.max_retry:
        r.profile_status = FAILED
        r.pending_since = None
        r.next_retry_at = None
        return False
    delay = retry_after_s if (status == 503 and retry_after_s is not None) else policy.backoff_s
    r.next_retry_at = now + delay
    return True


# ---------------------------------------------------------------------------
# 사건 — 7.7 콜백을 받음 (§3.4 ③)
# ---------------------------------------------------------------------------


def on_callback(r: Recipient, body_version: int, product_ids: list[int]) -> tuple[int, str | None]:
    """(HTTP 상태, 오류 코드). 낡은 결과가 최신을 덮는 것만 거부하고, 같은 번호의 재전송은 그대로 다시 저장한다."""
    if body_version < r.analyzed_source_version:
        return 409, "STALE_SOURCE_VERSION"
    r.recommended_product_ids = list(product_ids)
    r.analyzed_source_version = body_version
    r.retry_count = 0
    r.next_retry_at = None
    if body_version == r.source_version:            # 마지막으로 보낸 번호의 결과 = 최신
        r.profile_status = COMPLETED
        r.pending_since = None
    # 더 낮은 번호면 새 요청이 진행 중 — 30개는 갱신하되 PENDING 유지
    return 200, None


# ---------------------------------------------------------------------------
# 사건 — PENDING 타임아웃 (§3.4 ④)
# ---------------------------------------------------------------------------


def expire(r: Recipient, now: float, policy: Policy) -> bool:
    """재시도를 다 썼고 대기 중인 수정도 없으면 FAILED. 바꿨으면 True."""
    if (r.profile_status == PENDING and r.pending_since is not None
            and now - r.pending_since >= policy.pending_timeout_s
            and r.retry_count >= policy.max_retry and r.last_changed_at is None):
        r.profile_status = FAILED
        r.pending_since = None
        r.next_retry_at = None
        return True
    return False
