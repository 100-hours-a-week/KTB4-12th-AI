"""접수 단계 중복 판정 — Backend가 같은 sourceVersion으로 재전송할 때(10분 PENDING 타임아웃, 최대 2회) 무엇을 하는가.

DB도 앱도 띄우지 않는다: decide()는 순수 함수이고, 라우터는 ports 모양의 가짜를 넣어 직접 부른다.
판정 기준은 두 가지뿐 — **분석이 몇 번 돌았나**와 **콜백이 몇 번 나갔나**.
"""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import anyio
import pytest
from fastapi import HTTPException

from profiling import intake, pipeline
from profiling.intake import ANALYZE, RESEND, SKIP, decide
from profiling.schemas import ProfileExtractRequest
from profiling.settings import Settings
from profiling.types import (
    CallbackResult,
    ErrorCode,
    ProfileOutcome,
    RunStatus,
    SearchResult,
)

CV = UUID("11111111-2222-3333-4444-555555555555")
RID, SV = 4242, 7


def _body(**over) -> ProfileExtractRequest:
    return ProfileExtractRequest(**{"recipientUserId": RID, "sourceVersion": SV,
                                    "dislikedCategories": [{"categoryId": 9, "categoryName": "주방"}],
                                    "giftPreference": None, "reviews": [], **over})


## 기존 기록의 해시는 **실제 본문 해시**여야 한다 — 그래야 "같은 요청의 재전송"으로 판정된다
HASH = pipeline.input_hash(pipeline.to_internal(_body()))


def _outcome(status: RunStatus, *, input_hash: str = HASH, age_s: float | None = 0, search: bool = True) -> ProfileOutcome:
    return ProfileOutcome(
        recipient_user_id=RID, source_version=SV, status=status, input_hash=input_hash,
        search=SearchResult(product_ids=[1, 2, 3], query_text="", catalog_version_id=CV) if search else None,
        callback_attempts=1 if status is not RunStatus.RUNNING else 0,
        updated_at=None if age_s is None else datetime.now(UTC) - timedelta(seconds=age_s),
    )


# ---------------------------------------------------------------- decide() — 판정만


@pytest.mark.parametrize(("existing", "expected"), [
    (None, ANALYZE),                                                    # 첫 접수
    (_outcome(RunStatus.RUNNING), SKIP),                                # 이미 돌고 있다
    (_outcome(RunStatus.RUNNING, age_s=9999), ANALYZE),                 # 죽은 실행이 남긴 행
    (_outcome(RunStatus.RUNNING, age_s=None), SKIP),                    # 시각을 모르면 신선한 것으로 본다
    (_outcome(RunStatus.RESULT_READY), RESEND),                         # 결과 있음 — 재분석 없이 다시 보낸다
    (_outcome(RunStatus.DELIVERED), RESEND),                            # Backend가 못 받았을 수 있다
    (_outcome(RunStatus.SUPERSEDED), RESEND),
    (_outcome(RunStatus.FAILED, search=False), ANALYZE),                # 지난 실행 실패 → 다시
    (_outcome(RunStatus.RESULT_READY, search=False), ANALYZE),          # 결과 상태인데 본문이 없다(방어)
    (_outcome(RunStatus.RESULT_READY, input_hash="b" * 64), ANALYZE),   # 같은 번호·다른 본문
])
def test_decide(existing, expected) -> None:
    action, why = decide(existing, HASH, stale_after_s=300)
    assert action == expected and why


def test_decide_stale_boundary() -> None:
    """기준(초)을 넘겨야 죽은 것으로 본다 — 딱 걸치면 아직 살아 있는 것으로. 시각을 넘겨 재현 가능하게 한다."""
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    run = _outcome(RunStatus.RUNNING)
    at = lambda s: run.model_copy(update={"updated_at": now - timedelta(seconds=s)})
    assert decide(at(300), HASH, stale_after_s=300, now=now)[0] == SKIP
    assert decide(at(301), HASH, stale_after_s=300, now=now)[0] == ANALYZE


# ---------------------------------------------------------------- 라우터 — 어느 경로로 가는가


class FakeCatalog:
    def active(self):
        return CV, []


class FakeStore:
    def __init__(self, existing=None, lock_free=True):
        self.existing, self.saved, self.lock_free = existing, [], lock_free
        self.locked = []

    def save(self, outcome):
        self.saved.append(outcome)

    def get(self, rid):
        return self.existing

    def get_run(self, rid, sv):
        return self.existing if (rid, sv) == (RID, SV) else None

    @contextmanager
    def run_lock(self, rid, sv):
        """lock_free=False 면 "다른 실행이 처리 중"을 흉내 낸다."""
        self.locked.append((rid, sv))
        yield self.lock_free


class FakeBackend:
    def __init__(self):
        self.sent = []

    def send_profile_callback(self, outcome):
        self.sent.append(outcome)
        return CallbackResult(status=RunStatus.DELIVERED, http_status=200)


class FakeSupervisor:
    """무엇을 제출했는지만 기록한다. run()으로 그 작업을 실제로 돌린다. accept=False 면 큐 가득을 흉내 낸다."""

    def __init__(self, accept: bool = True):
        self.submitted = []
        self.accept = accept

    def submit(self, fn, *args, **kwargs):
        if not self.accept:
            return False
        self.submitted.append((fn, args, kwargs))
        return True

    def run(self):
        for fn, args, kwargs in self.submitted:
            fn(*args, **kwargs)


def _post(existing, monkeypatch, *, lock_free=True, store=None) -> tuple[list[str], FakeStore, FakeBackend]:
    """7.6 한 번 보내고 → 슬롯에 올라간 작업을 실행한다. 실제로 **무엇이 돌았는지**를 목록으로 돌려준다.

    분석·재전송 자체는 다른 시험이 덮으므로 여기서는 기록만 한다(판정이 관심사).
    """
    store = store or FakeStore(existing, lock_free=lock_free)
    backend, sup, done = FakeBackend(), FakeSupervisor(), []
    monkeypatch.setattr(intake, "run_and_callback", lambda *a, **k: done.append("analyze"))
    monkeypatch.setattr(intake, "resend_callback", lambda outcome, *a, **k: done.append(("resend", outcome)))

    res = asyncio.run(intake.extract_and_pool(
        body=_body(), catalog=FakeCatalog(), store=store, recipient_store=object(),
        backend=backend, supervisor=sup, settings=Settings(), io_limiter=anyio.CapacityLimiter(1),
    ))
    assert res.data.profileStatus == "PENDING" and res.data.sourceVersion == SV   # 어느 경로든 응답은 같다
    assert [fn for fn, _, _ in sup.submitted] == [intake.dispatch]                # 접수는 판정하지 않는다
    sup.run()
    return done, store, backend


def test_first_request_runs_analysis(monkeypatch) -> None:
    done, store, _ = _post(None, monkeypatch)
    assert done == ["analyze"] and store.locked == [(RID, SV)]      # 판정 전에 잠갔다


def test_running_is_not_analyzed_again(monkeypatch) -> None:
    """이미 분석 중이면 아무것도 하지 않는다 — Backend 재전송이 분석을 두 번 돌리지 못하게."""
    done, _, backend = _post(_outcome(RunStatus.RUNNING), monkeypatch)
    assert done == [] and backend.sent == []


def test_existing_result_is_resent_without_reanalysis(monkeypatch) -> None:
    existing = _outcome(RunStatus.RESULT_READY)
    done, _, _ = _post(existing, monkeypatch)
    assert [k for k, *_ in done] == ["resend"]
    assert done[0][1] is existing                                    # 저장된 그 결과를 그대로 보낸다


def test_failed_run_is_analyzed_again(monkeypatch) -> None:
    done, _, _ = _post(_outcome(RunStatus.FAILED, search=False), monkeypatch)
    assert done == ["analyze"]


def test_same_version_different_body_is_analyzed_again(monkeypatch) -> None:
    done, _, _ = _post(_outcome(RunStatus.DELIVERED, input_hash="b" * 64), monkeypatch)
    assert done == ["analyze"]


def test_lock_held_elsewhere_does_nothing(monkeypatch) -> None:
    """같은 (수신자, 버전)을 다른 실행이 잡고 있으면 판정도 실행도 하지 않는다.

    이 잠금이 없으면 동시에 들어온 두 요청이 둘 다 "기록 없음"을 보고 둘 다 분석한다.
    """
    done, store, backend = _post(None, monkeypatch, lock_free=False)
    assert done == [] and backend.sent == [] and store.locked == [(RID, SV)]


def test_lookup_failure_does_not_block_intake(monkeypatch) -> None:
    """중복 판정용 조회가 깨져도 접수는 계속된다 — 분석을 한 번 더 돌릴 뿐."""
    class BrokenStore(FakeStore):
        def get_run(self, rid, sv):
            raise RuntimeError("DB 끊김")

    done, _, _ = _post(None, monkeypatch, store=BrokenStore())
    assert done == ["analyze"]


def test_resend_records_callback_failure() -> None:
    """재전송이 실패해도 그 결과가 기록된다 (RESULT_READY로 남아 다음 재전송 대상)."""
    class Unreachable(FakeBackend):
        def send_profile_callback(self, outcome):
            self.sent.append(outcome)
            return CallbackResult(status=RunStatus.RESULT_READY, code=ErrorCode.CALLBACK_UNREACHABLE, message="타임아웃")

    existing, store, backend = _outcome(RunStatus.RESULT_READY), FakeStore(), Unreachable()
    intake.resend_callback(existing, backend, store, max_attempts=1)          # 즉시 재시도는 test_callback_retry 가 본다
    assert store.saved[0].status is RunStatus.RESULT_READY
    assert store.saved[0].failure_code is ErrorCode.CALLBACK_UNREACHABLE
    assert store.saved[0].callback_attempts == existing.callback_attempts + 1


# ---------------------------------------------------------------- 큐 가득 (09-28)


def test_queue_full_is_rejected_with_503_and_short_retry_after() -> None:
    """Supervisor 가 받지 못하면(큐 가득·종료 중) 기다리지 않고 503 — Retry-After 는 QUEUE_FULL_RETRY_AFTER_S(30), 카탈로그 없음의 300이 아니다."""
    sup = FakeSupervisor(accept=False)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(intake.extract_and_pool(
            body=_body(), catalog=FakeCatalog(), store=FakeStore(None), recipient_store=object(),
            backend=FakeBackend(), supervisor=sup, settings=Settings(), io_limiter=anyio.CapacityLimiter(1),
        ))
    assert ei.value.status_code == 503
    assert ei.value.detail["code"] == "SERVICE_UNAVAILABLE" and ei.value.detail["retryAfter"] == Settings().QUEUE_FULL_RETRY_AFTER_S
    assert sup.submitted == []                                     # 아무것도 실행되지 않는다
