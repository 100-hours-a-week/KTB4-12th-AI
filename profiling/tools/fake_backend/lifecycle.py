"""페이크 Backend의 상태 기계 — 모델 API 설계 v3.2.7 §7.6·§7.7 과 BE_연동_필드표 v1 §3 을 그대로 옮긴 것.

**판단만 한다.** HTTP도 시간도 여기서 만들지 않는다(`now`를 받는다) — 그래야 DB·서버 없이 시험한다.
실제 Backend가 할 일을 흉내 내는 것이 목적이고, 시험에서는 디바운스·타임아웃을 초 단위로 줄여 쓴다.

상태가 바뀌는 곳은 셋뿐이다(필드표 §3.3): 202 → PENDING · 7.7 200 → COMPLETED · 타임아웃 → FAILED.
오류 응답은 상태를 바꾸지 않는다.

09-25 합의(재전송): PENDING이 타임아웃을 넘기면 **같은 sourceVersion으로 최대 2회** 다시 보낸다.
  retry_count 는 재전송 때 +1, 새 번호로 보낼 때와 7.7 결과를 저장했을 때 0.
  대기 중인 수정이 있으면(last_changed_at) 재전송 대신 다음 7.6(새 번호)이 나가므로 재전송하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NONE, PENDING, COMPLETED, FAILED = "NONE", "PENDING", "COMPLETED", "FAILED"
SEND_NEW, SEND_RETRY = "new", "retry"


@dataclass(frozen=True)
class Policy:
    """실제 Backend 값은 디바운스 1시간·상한 6시간·타임아웃 10분. 시험에서는 초 단위로 줄인다."""

    debounce_s: float = 5.0            # 마지막 변경 후 이만큼 잠잠하면 보낸다
    window_s: float = 30.0             # 첫 미반영 변경 후 이만큼 지나면 잠잠하지 않아도 보낸다
    pending_timeout_s: float = 10.0    # PENDING이 이보다 오래가면 재전송 또는 FAILED
    max_retry: int = 2                 # 같은 번호로 다시 보내는 최대 횟수


@dataclass
class Recipient:
    """Backend가 수신자마다 들고 있어야 하는 값 (필드표 §3.2)."""

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

    def snapshot(self) -> dict:
        return {"recipientUserId": self.recipient_user_id, "profileStatus": self.profile_status,
                "sourceVersion": self.source_version, "analyzedSourceVersion": self.analyzed_source_version,
                "retryCount": self.retry_count, "recommendedCount": len(self.recommended_product_ids),
                "pendingWaiting": self.last_changed_at is not None}


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
# 판단 — 지금 7.6 을 보내야 하나 (§3.4 ① · ⑤)
# ---------------------------------------------------------------------------


def due_send(r: Recipient, now: float, policy: Policy) -> str | None:
    """SEND_NEW(새 번호) · SEND_RETRY(같은 번호 재전송) · None.

    **대기 중인 수정이 있으면 재전송하지 않는다** — 어차피 새 번호가 나가므로 낡은 입력으로 재시도할 이유가 없다.
    """
    if r.last_changed_at is not None:
        quiet = now - r.last_changed_at >= policy.debounce_s
        capped = r.window_started_at is not None and now - r.window_started_at >= policy.window_s
        return SEND_NEW if (quiet or capped) else None
    if (r.profile_status == PENDING and r.pending_since is not None
            and now - r.pending_since >= policy.pending_timeout_s and r.retry_count < policy.max_retry):
        return SEND_RETRY
    return None


def start_new(r: Recipient) -> int:
    """번호를 올려 저장하고(요청보다 먼저) 보낼 번호를 돌려준다. 재시도 횟수는 새 번호마다 0."""
    r.source_version += 1
    r.retry_count = 0
    return r.source_version


def start_retry(r: Recipient) -> int:
    """같은 번호로 다시 보낸다. 횟수만 올린다."""
    r.retry_count += 1
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
    if r.analyzed_source_version < sent_version:
        r.profile_status = PENDING
        r.pending_since = now
    if r.last_changed_at == changed_at_when_sent:
        r.last_changed_at = None
        r.window_started_at = None


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
        return True
    return False
