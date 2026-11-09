"""예외 메시지·연쇄 traceback을 기록하지 않고 저장·잠금 동작을 유지한다. 모두 합성 어댑터다.

옛 HTTP 7.6 접수 · 7.7 콜백 · 작업 큐 시험은 11-08 그 경로를 지우며 함께 지웠다(MQ 처리 · 로그는 test_mq_*.py)."""

import asyncio
import json
import sys

import pytest
import sqlalchemy as sa
from starlette.requests import Request

from profiling import main, stores
from tests.unit.test_stores_lock_key import _RecordingConn, _RecordingEngine

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

