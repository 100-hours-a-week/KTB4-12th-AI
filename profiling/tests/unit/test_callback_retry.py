"""7.7 콜백 즉시 재시도 — 5xx·네트워크만, 같은 슬롯 안에서 짧게 (BE 계획 5-5 "콜백 저장 실패 후 AI의 동일 콜백 재전송"에 대한 우리 쪽 답).

DB·HTTP 없음. BackendPort 모양의 가짜가 응답 순서를 정하고, sleep 은 기록만 한다.
규칙: RESULT_READY(5xx·타임아웃·연결 실패)만 다시 보낸다 · 대기 0.5초 → 2초 · 최대 3회 · 200/409/4xx 는 그 자리에서 끝.
"""

from datetime import UTC, datetime
from uuid import UUID

from profiling import intake
from profiling.types import (
    CallbackResult,
    ErrorCode,
    ProfileOutcome,
    RunStatus,
    SearchResult,
)

CV = UUID(int=5)
UNREACHABLE = CallbackResult(status=RunStatus.RESULT_READY, http_status=503, code=ErrorCode.CALLBACK_UNREACHABLE,
                             message="503 SERVICE_UNAVAILABLE")
DELIVERED = CallbackResult(status=RunStatus.DELIVERED, http_status=200)
STALE = CallbackResult(status=RunStatus.SUPERSEDED, http_status=409, code=ErrorCode.CALLBACK_STALE, message="409 STALE_SOURCE_VERSION")
REJECTED = CallbackResult(status=RunStatus.FAILED, http_status=400, code=ErrorCode.CONTRACT_7_7_REJECTED, message="400 INVALID_REQUEST")


def _outcome(attempts: int = 1) -> ProfileOutcome:
    return ProfileOutcome(recipient_user_id=1, source_version=1, status=RunStatus.RESULT_READY, input_hash="h" * 64,
                          search=SearchResult(product_ids=[1, 2, 3], query_text="", catalog_version_id=CV),
                          callback_attempts=attempts, updated_at=datetime.now(UTC))


class ScriptedBackend:
    """응답을 순서대로 돌려준다. 몇 번 불렸는지가 관심사."""

    def __init__(self, *results: CallbackResult):
        self.results, self.sent = list(results), []

    def send_profile_callback(self, outcome):
        self.sent.append(outcome)
        return self.results.pop(0)


class Store:
    def __init__(self):
        self.saved = []

    def save(self, outcome):
        self.saved.append(outcome)


def _run(*results: CallbackResult, max_attempts: int = 3):
    backend, store, waits = ScriptedBackend(*results), Store(), []
    intake._send_and_record(_outcome(attempts=1), backend, store, max_attempts=max_attempts, backoff_s=0.5, sleep=waits.append)
    assert len(store.saved) == 1                                            # 저장은 마지막에 한 번
    return backend, store.saved[0], waits


def test_5xx_twice_then_200_is_delivered_after_two_waits() -> None:
    backend, saved, waits = _run(UNREACHABLE, UNREACHABLE, DELIVERED)
    assert len(backend.sent) == 3 and waits == [0.5, 2.0]                   # 0.5초 → 2초(4배)
    assert saved.status is RunStatus.DELIVERED and saved.failure_code is None
    assert saved.callback_attempts == 1 + 3                                 # 실제로 보낸 만큼 더한다


def test_5xx_three_times_stays_result_ready_for_backend_retry() -> None:
    """다 써도 실패면 RESULT_READY 로 남는다 — Backend 가 같은 번호로 다시 요청할 때 재전송(decide → RESEND)."""
    backend, saved, waits = _run(UNREACHABLE, UNREACHABLE, UNREACHABLE)
    assert len(backend.sent) == 3 and waits == [0.5, 2.0]                   # 마지막 실패 뒤에는 기다리지 않는다
    assert saved.status is RunStatus.RESULT_READY and saved.failure_code is ErrorCode.CALLBACK_UNREACHABLE
    assert saved.callback_attempts == 4


def test_409_stops_immediately() -> None:
    backend, saved, waits = _run(STALE)
    assert len(backend.sent) == 1 and waits == [] and saved.status is RunStatus.SUPERSEDED


def test_4xx_stops_immediately() -> None:
    backend, saved, waits = _run(REJECTED)
    assert len(backend.sent) == 1 and waits == [] and saved.status is RunStatus.FAILED
    assert saved.failure_code is ErrorCode.CONTRACT_7_7_REJECTED


def test_max_attempts_one_means_no_retry() -> None:
    backend, saved, waits = _run(UNREACHABLE, max_attempts=1)
    assert len(backend.sent) == 1 and waits == [] and saved.callback_attempts == 2


def test_resend_path_retries_the_same_way() -> None:
    """Backend 재요청으로 들어온 재전송도 같은 경로 — 5xx 면 짧게 다시 보낸다."""
    backend, store, waits = ScriptedBackend(UNREACHABLE, DELIVERED), Store(), []
    intake.resend_callback(_outcome(attempts=1), backend, store, sleep=waits.append)
    assert len(backend.sent) == 2 and waits == [0.5]
    assert store.saved[0].status is RunStatus.DELIVERED and store.saved[0].callback_attempts == 3
