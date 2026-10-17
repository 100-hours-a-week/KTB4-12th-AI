"""예외 메시지·연쇄 traceback을 기록하지 않고 저장·잠금·콜백 동작을 유지한다. 모두 합성 어댑터다."""

import asyncio
import json
import sys
import threading

import httpx
import pytest
import sqlalchemy as sa
from starlette.requests import Request

from profiling import backend as backend_module
from profiling import intake, main, pipeline, stores
from profiling.supervisor import Supervisor
from profiling.types import RunStatus
from tests.unit.test_callback_retry import (
    DELIVERED,
    REJECTED,
    STALE,
    UNREACHABLE,
    ScriptedBackend,
    Store,
    _outcome,
)
from tests.unit.test_intake_dedupe import FakeBackend, FakeCatalog, FakeStore, _body
from tests.unit.test_stores_lock_key import _RecordingConn, _RecordingEngine
from tests.unit.test_supervisor import _until

PRIVATE = "SYNTHETIC_PRIVATE_EXCEPTION_97531"
UNLOCK_PRIVATE = "SYNTHETIC_PRIVATE_UNLOCK_97531"


def _assert_private(caplog, error_type=None):
    records = [record for record in caplog.records if record.name.startswith("profiling.")]
    assert PRIVATE not in caplog.text and UNLOCK_PRIVATE not in caplog.text
    assert all(record.exc_info is None and record.exc_text is None for record in records)
    if error_type:
        assert any(error_type in record.getMessage() for record in records)


@pytest.mark.parametrize("save_fails", [False, True])
@pytest.mark.parametrize("unlock_fails", [False, True])
def test_save_locked_omits_chained_exception_and_preserves_cleanup(save_fails, unlock_fails, caplog):
    save_error = sa.exc.IntegrityError("synthetic save", {"pref_clean": PRIVATE}, ValueError(PRIVATE))
    unlock_error = sa.exc.OperationalError("synthetic unlock", {}, RuntimeError(UNLOCK_PRIVATE))
    active_at_unlock = []

    class Connection(_RecordingConn):
        def execute(self, statement, params=None):
            result = super().execute(statement, params)
            if statement is stores._PROFILE_SAVE and save_fails:
                raise save_error
            if statement is stores._PROFILE_UNLOCK:
                active_at_unlock.append(sys.exception())
                if unlock_fails:
                    raise unlock_error
            return result

    conn = Connection()
    with caplog.at_level("DEBUG", logger="profiling"):
        if save_fails:
            with pytest.raises(sa.exc.IntegrityError) as caught:
                stores._save_locked(conn, 7, {"pref_clean": PRIVATE})
            assert caught.value is save_error
        else:
            assert stores._save_locked(conn, 7, {"pref_clean": PRIVATE}) == 1
    assert active_at_unlock == [save_error if save_fails else None]
    expected = [str(stores._PROFILE_LOCK), str(stores._PROFILE_SAVE), str(stores._PROFILE_UNLOCK)]
    assert conn.calls == expected + (["invalidate"] if unlock_fails else [])
    assert not conn.closed  # 이 함수의 호출자가 커넥션 수명을 소유한다.
    if save_fails and unlock_fails:
        assert unlock_error.__context__ is save_error  # 재현 조건: 해제 예외의 traceback이 저장 예외까지 포함한다.
    _assert_private(caplog, "OperationalError" if unlock_fails else None)


@pytest.mark.parametrize("stage", ["connect", "options", "lock"])
def test_run_lock_failure_keeps_fallback_without_exception_text(stage, monkeypatch, caplog):
    engine = _RecordingEngine()

    def fail(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    target, attr = {"connect": (engine, "connect"), "options": (engine.conn, "execution_options"),
                    "lock": (engine.conn, "execute")}[stage]
    monkeypatch.setattr(target, attr, fail)
    with caplog.at_level("DEBUG", logger="profiling"), stores.DbProfileRunStore(engine).run_lock(7, 1) as got:
        assert got is True and stores._lock_conn() is None
    assert engine.conn.closed is (stage != "connect")
    assert "invalidate" not in engine.conn.calls
    _assert_private(caplog, "RuntimeError")


@pytest.mark.parametrize("body_fails", [False, True])
def test_run_unlock_omits_chained_exception_and_keeps_invalidate(body_fails, monkeypatch, caplog):
    engine = _RecordingEngine()
    execute = engine.conn.execute
    body_error = RuntimeError(PRIVATE)
    unlock_error = sa.exc.OperationalError("synthetic unlock", {}, RuntimeError(UNLOCK_PRIVATE))

    def execute_or_fail(statement, params=None):
        result = execute(statement, params)
        if "pg_advisory_unlock" in str(statement):
            raise unlock_error
        return result

    monkeypatch.setattr(engine.conn, "execute", execute_or_fail)

    def run():
        with stores.DbProfileRunStore(engine).run_lock(7, 1) as got:
            assert got is True and stores._lock_conn() is engine.conn
            if body_fails:
                raise body_error

    with caplog.at_level("DEBUG", logger="profiling"):
        if body_fails:
            with pytest.raises(RuntimeError) as caught:
                run()
            assert caught.value is body_error and unlock_error.__context__ is body_error
        else:
            run()
    assert stores._lock_conn() is None and engine.conn.closed
    assert engine.conn.calls == ["execution_options", "select pg_try_advisory_lock(:k)", "select pg_advisory_unlock(:k)", "invalidate"]
    _assert_private(caplog, "OperationalError")


@pytest.mark.parametrize("results,waits", [
    ([DELIVERED], []), ([STALE], []), ([REJECTED], []),
    ([UNREACHABLE, UNREACHABLE, DELIVERED], [0.5, 2.0]),
    ([UNREACHABLE, UNREACHABLE, UNREACHABLE], [0.5, 2.0]),
])
def test_callback_save_failure_keeps_attempts_and_status_without_exception_text(results, waits, caplog):
    backend, attempted, actual_waits = ScriptedBackend(*results), [], []
    original = _outcome(attempts=1)
    original_json = original.model_dump_json()

    class BrokenStore:
        def save(self, outcome):
            attempted.append(outcome)
            raise RuntimeError(PRIVATE)

    with caplog.at_level("DEBUG", logger="profiling"):
        intake.resend_callback(original, backend, BrokenStore(), sleep=actual_waits.append)
    assert backend.sent == [original] * len(results) and not backend.results
    assert actual_waits == waits and len(attempted) == 1
    saved = attempted[0]
    assert saved.status is results[-1].status and saved.callback_attempts == 1 + len(results)
    assert saved.failure_code is results[-1].code and saved.failure_reason == results[-1].message
    assert original.model_dump_json() == original_json
    _assert_private(caplog, "RuntimeError")


def test_intake_pipeline_exception_omits_message_without_callback(monkeypatch, caplog):
    store, backend = FakeStore(), FakeBackend()

    def fail(*args, **kwargs):
        raise RuntimeError(PRIVATE) from ValueError(PRIVATE)

    monkeypatch.setattr(pipeline, "profile", fail)
    with caplog.at_level("DEBUG", logger="profiling"):
        intake.run_and_callback(pipeline.to_internal(_body()), FakeCatalog(), store, backend, 30, object())
    assert not store.saved and not backend.sent
    _assert_private(caplog, "RuntimeError")


def test_intake_lookup_exception_keeps_analysis_without_exception_text(monkeypatch, caplog):
    class BrokenStore(FakeStore):
        def get_run(self, rid, sv):
            raise RuntimeError(PRIVATE)

    store, backend, analyzed = BrokenStore(), FakeBackend(), []
    rq = pipeline.to_internal(_body())
    monkeypatch.setattr(intake, "run_and_callback", lambda *args, **kwargs: analyzed.append(args[0]))
    with caplog.at_level("DEBUG", logger="profiling"):
        intake.dispatch(rq, FakeCatalog(), store, backend, 30, object(), stale_after_s=300)
    assert analyzed == [rq] and store.locked == [(rq.recipient_user_id, rq.source_version)]
    assert not store.saved and not backend.sent
    _assert_private(caplog, "RuntimeError")


@pytest.mark.parametrize("worker", [False, True])
def test_supervisor_omits_chained_exception_and_continues(worker, caplog):
    sup, done = Supervisor(profiling_slots=1, queue_max=2), threading.Event()

    def fail():
        raise RuntimeError(PRIVATE) from ValueError(PRIVATE)

    with caplog.at_level("DEBUG", logger="profiling"):
        assert sup.submit(fail) and sup.submit(done.set)
        try:
            if worker:
                sup.start()
                assert done.wait(1.0) and _until(lambda: sup.stats()["completed"] == 2)
            else:
                assert sup.run_pending() == 2 and done.is_set()
            assert sup.stats() == {"slots": 1, "running": 0, "queued": 0, "queue_max": 2,
                                   "submitted": 2, "completed": 2, "rejected": 0, "failed": 1}
        finally:
            sup.stop(1.0)
    _assert_private(caplog, "RuntimeError")


def test_http_unhandled_exception_omits_message_and_preserves_500(caplog):
    request = Request({"type": "http", "method": "POST", "path": "/synthetic", "headers": [], "query_string": b""})

    async def run():
        try:
            raise RuntimeError(PRIVATE) from ValueError(PRIVATE)
        except RuntimeError as error:
            return await main.on_unhandled(request, error)

    with caplog.at_level("DEBUG", logger="profiling"):
        response = asyncio.run(run())
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["error"] == {"code": "INTERNAL_SERVER_ERROR", "traceId": None}
    assert body["message"] == "서버 오류가 발생했습니다."
    _assert_private(caplog, "RuntimeError")


@pytest.mark.parametrize("stage", ["body", "network"])
def test_callback_adapter_omits_exception_text_in_logs_and_saved_result(stage, monkeypatch, caplog):
    sent, waits, store = [], [], Store()

    def send(request):
        sent.append(request)
        raise httpx.ConnectError(PRIVATE, request=request)

    if stage == "body":
        def fail(outcome):
            raise ValueError(PRIVATE)
        monkeypatch.setattr(backend_module, "to_callback", fail)
    backend = backend_module.HttpBackendPort("https://synthetic.invalid", "", 1.0, transport=httpx.MockTransport(send))
    try:
        with caplog.at_level("DEBUG", logger="profiling"):
            intake._send_and_record(_outcome(), backend, store, sleep=waits.append)
    finally:
        backend.close()
    assert len(store.saved) == 1
    saved = store.saved[0]
    if stage == "network":
        assert len(sent) == 3 and waits == [0.5, 2.0] and saved.callback_attempts == 4
        assert saved.status is RunStatus.RESULT_READY and saved.failure_code is UNREACHABLE.code
    else:
        assert not sent and not waits and saved.callback_attempts == 2
        assert saved.status is RunStatus.FAILED and saved.failure_code.value == "PIPELINE_ERROR"
    assert PRIVATE not in saved.model_dump_json()
    _assert_private(caplog, "ConnectError" if stage == "network" else "ValueError")
