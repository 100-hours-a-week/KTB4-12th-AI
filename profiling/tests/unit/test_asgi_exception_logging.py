"""포트·DB 없이 실제 main.app → Starlette → Uvicorn의 예외 재전파와 로그를 검증한다."""

import asyncio
import json
import logging

import h11
import pytest
from uvicorn.protocols.http.flow_control import FlowControl
from uvicorn.protocols.http.h11_impl import RequestResponseCycle

from profiling import main

PRIVATE = "SYNTHETIC_ASGI_PRIVATE_97531"
CAUSE_PRIVATE = "SYNTHETIC_ASGI_CAUSE_97531"


class MemoryTransport(asyncio.Transport):
    def __init__(self):
        self.writes = []
        self.closes = 0

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.closes += 1

    def is_closing(self):
        return bool(self.closes)


def test_actual_uvicorn_cycle_sanitizes_rethrown_exception_without_changing_response(monkeypatch, caplog):
    original = RuntimeError(PRIVATE)
    cause = ValueError(CAUSE_PRIVATE)

    async def fail():
        raise original from cause

    monkeypatch.setattr(main.app.router, "routes", list(main.app.router.routes))
    monkeypatch.setattr(main.app, "middleware_stack", None)
    main.app.add_api_route("/synthetic-error", fail, methods=["POST"])
    rethrown, completed = [], []

    async def observe(scope, receive, send):
        try:
            await main.app(scope, receive, send)
        except Exception as error:
            rethrown.append(error)
            raise

    async def run():
        transport = MemoryTransport()
        connection = h11.Connection(h11.SERVER)
        connection.receive_data(b"POST /synthetic-error HTTP/1.1\r\nhost: synthetic\r\ncontent-length: 0\r\n\r\n")
        assert isinstance(connection.next_event(), h11.Request)
        assert isinstance(connection.next_event(), h11.EndOfMessage)
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "scheme": "http", "method": "POST", "root_path": "", "path": "/synthetic-error",
                 "raw_path": b"/synthetic-error", "query_string": b"", "headers": [(b"host", b"synthetic")],
                 "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 2), "state": {}}
        event = asyncio.Event()
        event.set()
        cycle = RequestResponseCycle(scope, connection, transport, FlowControl(transport), logging.getLogger("uvicorn.error"),
                                     logging.getLogger("uvicorn.access"), False, [], event, lambda: completed.append(True))
        cycle.more_body = False
        await cycle.run_asgi(observe)
        return transport, cycle

    with caplog.at_level("DEBUG", logger="uvicorn.error"):
        transport, cycle = asyncio.run(run())
    assert rethrown == [original] and original.__cause__ is cause
    assert cycle.response_started and cycle.response_complete
    assert transport.closes == 1 and completed == [True]
    headers, body = b"".join(transport.writes).split(b"\r\n\r\n", 1)
    assert headers.startswith(b"HTTP/1.1 500 ")
    assert json.loads(body) == {"message": "서버 오류가 발생했습니다.", "error": {"code": "INTERNAL_SERVER_ERROR", "traceId": None}}
    assert PRIVATE not in caplog.text and CAUSE_PRIVATE not in caplog.text
    records = [record for record in caplog.records if record.name == "uvicorn.error"]
    assert len(records) == 1 and records[0].levelno == logging.ERROR
    assert "RuntimeError" in records[0].getMessage()
    assert records[0].exc_info is None and records[0].exc_text is None


@pytest.mark.parametrize("cached,with_exception", [(False, True), (True, True), (True, False)])
def test_asgi_record_clears_exception_and_cached_text(cached, with_exception, caplog):
    logger = logging.getLogger("uvicorn.error")
    try:
        raise RuntimeError(PRIVATE) from ValueError(CAUSE_PRIVATE)
    except RuntimeError as error:
        info = (type(error), error, error.__traceback__)
    record = logger.makeRecord("uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application\n", (), info)
    if cached:
        logging.Formatter().format(record)  # 앞선 formatter가 만들어 둔 traceback 캐시도 지운다.
        assert PRIVATE in record.exc_text and CAUSE_PRIVATE in record.exc_text
        record.message = PRIVATE
        record.stack_info = PRIVATE
    if not with_exception:
        record.exc_info = None
    with caplog.at_level("DEBUG", logger="uvicorn.error"):
        logger.handle(record)
    assert PRIVATE not in caplog.text and CAUSE_PRIVATE not in caplog.text
    assert record.exc_info is None and record.exc_text is None and record.stack_info is None
    expected_type = "RuntimeError" if with_exception else "UnknownException"
    assert record.getMessage() == record.message == f"Exception in ASGI application: {expected_type}"
    assert record.args == (expected_type,)


@pytest.mark.parametrize("message,args,with_exception", [
    ("Started server process [%d]", (42,), False),
    ("ASGI callable returned without completing response.", (), False),
    ("ASGI callable should return None, but returned '%s'.", ("synthetic-result",), False),
    ("Server diagnostic: %s", ("synthetic-diagnostic",), True),
])
def test_other_uvicorn_diagnostics_are_unchanged(message, args, with_exception, caplog):
    logger = logging.getLogger("uvicorn.error")
    error = RuntimeError("synthetic-server-diagnostic")
    info = (type(error), error, None) if with_exception else None
    record = logger.makeRecord("uvicorn.error", logging.ERROR, __file__, 1, message, args, info)
    logging.Formatter().format(record)
    before = (record.msg, record.args, record.exc_info, record.exc_text, record.stack_info, record.getMessage())
    with caplog.at_level("DEBUG", logger="uvicorn.error"):
        logger.handle(record)
    assert (record.msg, record.args, record.exc_info, record.exc_text, record.stack_info, record.getMessage()) == before
    assert record in caplog.records
