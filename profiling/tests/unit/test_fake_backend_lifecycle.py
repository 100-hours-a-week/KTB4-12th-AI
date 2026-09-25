"""페이크 Backend 상태 기계 — 시간만 바꿔가며 전이를 확인한다 (HTTP·DB 없음).

이 표가 맞아야 시나리오 시험이 의미를 갖는다. 실제 Backend 규칙(필드표 v1 §3)과 같은지 여기서 고정한다.
"""

import pytest

from tools.fake_backend import lifecycle as lc

P = lc.Policy(debounce_s=5, window_s=30, pending_timeout_s=10, max_retry=2)
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
    r = _r(source_version=7, retry_count=2)
    assert lc.start_new(r) == 8 and r.retry_count == 0


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
    assert lc.start_retry(r) == 4 and r.retry_count == 1                    # 같은 번호
    r.pending_since = 110
    assert lc.due_send(r, now=120, policy=P) == lc.SEND_RETRY
    assert lc.start_retry(r) == 4 and r.retry_count == 2
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
