"""페이크 Backend 상태 기계 — 시간만 바꿔가며 전이를 확인한다 (HTTP·DB 없음).

이 표가 맞아야 시나리오 시험이 의미를 갖는다. 실제 Backend 규칙(필드표 v1 §3)과 같은지 여기서 고정한다.
"""

import pytest

from tools.fake_backend import lifecycle as lc

P = lc.Policy(debounce_s=5, window_s=30, pending_timeout_s=10, max_retry=2, backoff_s=2)
CATS = [{"categoryId": 12, "categoryName": "메이크업"}]


def _r(**over) -> lc.Recipient:
    r = lc.Recipient(recipient_user_id=1)
    for k, v in over.items():
        setattr(r, k, v)
    return r


# ---------------------------------------------------------------- 보낼 때 (§3.4 ①)


def test_change_does_not_touch_status_or_version() -> None:
    r = _r(profile_status=lc.COMPLETED, source_version=3, analyzed_source_version=3)
    lc.on_change(r, CATS, now=100)
    assert r.profile_status == lc.COMPLETED and r.source_version == 3      # 화면은 옛 30개를 계속 보여준다
    assert r.last_changed_at == 100 and r.window_started_at == 100


def test_debounce_waits_until_quiet() -> None:
    r = _r()
    lc.on_change(r, CATS, now=100)
    assert lc.due_send(r, now=104, policy=P) is None                        # 아직 잠잠하지 않다
    assert lc.due_send(r, now=105, policy=P) == lc.SEND_NEW


def test_window_cap_sends_even_if_user_keeps_editing() -> None:
    """계속 고쳐도 상한(window)을 넘으면 보낸다 — 영영 안 나가는 일이 없게."""
    r = _r()
    lc.on_change(r, CATS, now=100)
    for t in range(101, 131, 2):                                            # 2초마다 계속 수정
        lc.on_change(r, CATS, now=t)
    assert lc.due_send(r, now=129, policy=P) is None
    assert lc.due_send(r, now=130, policy=P) == lc.SEND_NEW                 # 첫 변경 + 30초


def test_start_new_bumps_version_and_resets_retry() -> None:
    r = _r(source_version=7, retry_count=2, next_retry_at=50)
    assert lc.start_new(r, now=100) == 8 and r.retry_count == 0
    assert r.next_retry_at is None and r.last_attempt_at == 100


def test_start_new_consumes_pending_change() -> None:
    """새 번호로 보낼 준비를 하면 대기 중인 수정은 소비된다(요청 스냅샷) — 실패해도 새 번호가 또 나가지 않게."""
    r = _r()
    lc.on_change(r, CATS, now=100)
    lc.start_new(r, now=105)
    assert r.last_changed_at is None and r.window_started_at is None
    assert lc.due_send(r, now=999, policy=P) is None                        # 보낼 것도, 재시도할 것도 없다


# ---------------------------------------------------------------- 202 (§3.4 ②)


def test_accept_sets_pending_and_clears_pending_change() -> None:
    r = _r(source_version=1)
    lc.on_change(r, CATS, now=100)
    sent_at_value = r.last_changed_at
    lc.on_accepted(r, sent_version=1, changed_at_when_sent=sent_at_value, now=110)
    assert r.profile_status == lc.PENDING and r.pending_since == 110
    assert r.last_changed_at is None                                        # 그 사이 수정이 없었으므로 비운다


def test_accept_keeps_change_that_arrived_while_sending() -> None:
    """보내는 사이에 들어온 수정은 살아남아야 한다 — 안 그러면 그 수정이 대기열에서 사라진다."""
    r = _r(source_version=1)
    lc.on_change(r, CATS, now=100)
    snapshot = r.last_changed_at
    lc.on_change(r, CATS, now=108)                                          # 요청 도중 또 수정
    lc.on_accepted(r, sent_version=1, changed_at_when_sent=snapshot, now=110)
    assert r.last_changed_at == 108                                         # 남아 있다 → 다음 7.6 이 나간다


def test_accept_does_not_overwrite_already_completed() -> None:
    """7.7 이 202 보다 먼저 처리된 경우 — COMPLETED 를 PENDING 으로 덮으면 10분 뒤 FAILED 로 오판한다."""
    r = _r(profile_status=lc.COMPLETED, source_version=2, analyzed_source_version=2)
    lc.on_accepted(r, sent_version=2, changed_at_when_sent=None, now=110)
    assert r.profile_status == lc.COMPLETED and r.pending_since is None


# ---------------------------------------------------------------- 7.7 (§3.4 ③)


def test_callback_latest_completes() -> None:
    r = _r(profile_status=lc.PENDING, source_version=3, pending_since=100, retry_count=1)
    assert lc.on_callback(r, body_version=3, product_ids=[1, 2, 3]) == (200, None)
    assert r.profile_status == lc.COMPLETED and r.analyzed_source_version == 3
    assert r.recommended_product_ids == [1, 2, 3] and r.retry_count == 0 and r.pending_since is None


def test_callback_older_than_saved_is_rejected() -> None:
    r = _r(profile_status=lc.PENDING, source_version=5, analyzed_source_version=4)
    assert lc.on_callback(r, body_version=3, product_ids=[9]) == (409, "STALE_SOURCE_VERSION")
    assert r.recommended_product_ids == []                                  # 낡은 결과가 최신을 덮지 않는다


def test_callback_for_older_but_unsaved_version_stays_pending() -> None:
    """분석 중에 사용자가 또 고쳐 다음 번호가 이미 나간 경우 — 30개는 갱신하되 PENDING 유지."""
    r = _r(profile_status=lc.PENDING, source_version=5, analyzed_source_version=3)
    assert lc.on_callback(r, body_version=4, product_ids=[7]) == (200, None)
    assert r.profile_status == lc.PENDING and r.recommended_product_ids == [7]


def test_resend_of_same_version_is_saved_again() -> None:
    r = _r(profile_status=lc.PENDING, source_version=2, analyzed_source_version=2)
    assert lc.on_callback(r, body_version=2, product_ids=[5, 6])[0] == 200
    assert r.profile_status == lc.COMPLETED and r.recommended_product_ids == [5, 6]


# ---------------------------------------------------------------- 재전송·타임아웃 (§3.4 ④⑤)


def test_retry_fires_twice_then_fails() -> None:
    """10분(여기서는 10초) PENDING → 같은 번호로 2회 재전송 → 그래도 없으면 FAILED."""
    r = _r(profile_status=lc.PENDING, source_version=4, pending_since=100)
    assert lc.due_send(r, now=109, policy=P) is None                        # 아직 타임아웃 전
    assert lc.due_send(r, now=110, policy=P) == lc.SEND_RETRY
    assert lc.start_retry(r, now=110) == 4 and r.retry_count == 1           # 같은 번호
    r.pending_since = 110
    assert lc.due_send(r, now=120, policy=P) == lc.SEND_RETRY
    assert lc.start_retry(r, now=120) == 4 and r.retry_count == 2
    r.pending_since = 120
    assert lc.due_send(r, now=130, policy=P) is None                        # 2회를 다 썼다
    assert lc.expire(r, now=130, policy=P) is True and r.profile_status == lc.FAILED


def test_pending_change_blocks_retry_and_expiry() -> None:
    """대기 중인 수정이 있으면 재전송도 FAILED 도 없다 — 어차피 새 번호가 나간다."""
    r = _r(profile_status=lc.PENDING, source_version=4, pending_since=100, retry_count=2)
    lc.on_change(r, CATS, now=105)
    assert lc.due_send(r, now=200, policy=P) == lc.SEND_NEW                 # 재전송이 아니라 새 번호
    assert lc.expire(r, now=200, policy=P) is False and r.profile_status == lc.PENDING


def test_completed_never_expires() -> None:
    r = _r(profile_status=lc.COMPLETED, source_version=1, analyzed_source_version=1, pending_since=None)
    assert lc.expire(r, now=99999, policy=P) is False


@pytest.mark.parametrize("status", [lc.NONE, lc.FAILED])
def test_only_pending_is_retried(status) -> None:
    r = _r(profile_status=status, pending_since=100)
    assert lc.due_send(r, now=999, policy=P) is None


# ---------------------------------------------------------------- 접수 실패 → 같은 번호 재시도 (BE 5-1 · 5-2)


def _sent_new(now: float = 100) -> lc.Recipient:
    """수정 → 새 번호로 보낼 준비까지 끝난 수신자 (7.6 응답을 기다리는 상태)."""
    r = _r()
    lc.on_change(r, CATS, now=now - 10)
    lc.start_new(r, now=now)
    return r


def test_rejected_500_retries_same_version_after_backoff() -> None:
    r = _sent_new(now=100)
    assert lc.on_rejected(r, status=500, retry_after_s=None, now=100, policy=P) is True
    assert r.next_retry_at == 102 and r.profile_status == lc.NONE           # 상태는 그대로, 시각만
    assert lc.due_send(r, now=101, policy=P) is None
    assert lc.due_send(r, now=102, policy=P) == lc.SEND_RETRY
    assert lc.start_retry(r, now=102) == 1 and r.retry_count == 1           # 번호 1 그대로
    assert r.next_retry_at is None


def test_rejected_503_prefers_retry_after_over_backoff() -> None:
    r = _sent_new(now=100)
    lc.on_rejected(r, status=503, retry_after_s=30, now=100, policy=P)
    assert r.next_retry_at == 130                                           # Retry-After 30초
    r2 = _sent_new(now=100)
    lc.on_rejected(r2, status=503, retry_after_s=None, now=100, policy=P)
    assert r2.next_retry_at == 102                                          # 헤더 없으면 기본 백오프


def test_rejected_connection_failure_uses_backoff() -> None:
    r = _sent_new(now=100)
    lc.on_rejected(r, status=None, retry_after_s=None, now=100, policy=P)
    assert r.next_retry_at == 102


@pytest.mark.parametrize("status", [400, 401])
def test_rejected_contract_or_token_error_never_retries(status) -> None:
    r = _sent_new(now=100)
    assert lc.on_rejected(r, status=status, retry_after_s=None, now=100, policy=P) is False
    assert r.next_retry_at is None and r.profile_status == lc.NONE
    assert lc.due_send(r, now=9999, policy=P) is None


def test_rejections_exhaust_retries_then_fail() -> None:
    """새 번호 1회 + 같은 번호 재시도 2회가 전부 실패하면 FAILED."""
    r = _sent_new(now=100)
    for attempt_at in (100, 102):
        assert lc.on_rejected(r, status=500, retry_after_s=None, now=attempt_at, policy=P) is True
        assert lc.due_send(r, now=attempt_at + 2, policy=P) == lc.SEND_RETRY
        lc.start_retry(r, now=attempt_at + 2)
    assert r.retry_count == 2
    assert lc.on_rejected(r, status=500, retry_after_s=None, now=104, policy=P) is False
    assert r.profile_status == lc.FAILED and r.next_retry_at is None and r.pending_since is None
    assert lc.due_send(r, now=9999, policy=P) is None


def test_new_change_during_retry_wait_wins_with_new_version() -> None:
    """재시도를 기다리는 동안 사용자가 또 고치면 옛 번호 재시도는 버리고 새 번호가 나간다 (BE 5-2)."""
    r = _sent_new(now=100)
    lc.on_rejected(r, status=500, retry_after_s=None, now=100, policy=P)
    lc.on_change(r, CATS, now=101)
    assert lc.due_send(r, now=102, policy=P) is None                        # 재시도 시각이 왔지만 새 수정이 우선 (디바운스 대기)
    assert lc.due_send(r, now=106, policy=P) == lc.SEND_NEW
    assert lc.start_new(r, now=106) == 2 and r.retry_count == 0 and r.next_retry_at is None


def test_accepted_clears_scheduled_retry() -> None:
    r = _sent_new(now=100)
    lc.on_rejected(r, status=500, retry_after_s=None, now=100, policy=P)
    lc.on_accepted(r, sent_version=1, changed_at_when_sent=None, now=102)
    assert r.next_retry_at is None and r.profile_status == lc.PENDING
