"""Private request values stay out of validation, application and ASGI error logs."""
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
from uuid import UUID, uuid4

import h11
import httpx
import psycopg
import pytest
from uvicorn.protocols.http.flow_control import FlowControl
from uvicorn.protocols.http.h11_impl import RequestResponseCycle

from app import main
from app.errors import install_error_handlers
from app.models import PutTurn

PRIVATE = "SYNTHETIC_PRIVATE_ERROR_731"
CAUSE_PRIVATE = "SYNTHETIC_PRIVATE_CAUSE_731"


def nul_body(field):
    body = {"message": "합성 질문"}
    if field in {"message", "model"}:
        body[field] = PRIVATE + "\x00"
    elif field == "snapshotId":
        body["preparedContext"] = {"productIds": [101], "snapshotId": PRIVATE + "\x00"}
    else:
        body["recipientContext"] = {
            "recipientUserId": 42, "status": "AVAILABLE", "sourceVersion": 1,
            "giftPreference": "합성 취향", "dislikedCategories": [],
        }
        if field == "giftPreference":
            body["recipientContext"][field] = PRIVATE + "\x00"
        else:
            body["recipientContext"]["dislikedCategories"] = [{"categoryId": 11, "categoryName": PRIVATE + "\x00"}]
    return body


@pytest.mark.parametrize("field", ["message", "model", "giftPreference", "categoryName", "snapshotId"])
async def test_nul_turn_input_is_400_before_storage(field, monkeypatch, caplog):
    calls = []

    class UnusedStore:
        async def call(self, *args, **kwargs):
            calls.append(args)
            raise AssertionError("Invalid input reached storage")

    monkeypatch.setattr(main.app.state, "store", UnusedStore(), raising=False)
    path = f"{main.BASE}/{uuid4()}/turns/{uuid4()}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://synthetic") as client:
        response = await client.put(path, json=nul_body(field), headers={"Authorization": "Bearer " + main.TOKEN})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert UUID(response.json()["error"]["traceId"]).version == 4
    assert not calls
    assert PRIVATE not in response.text and PRIVATE not in caplog.text


def test_ordinary_unicode_and_whitespace_keep_existing_validation():
    assert PutTurn.model_validate({"message": "  한글 🎁\n합성  "}).message == "한글 🎁\n합성"


class MemoryTransport(asyncio.Transport):
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed


async def run_uvicorn_request(app, path, *, body=None):
    """Run the actual Uvicorn/Starlette exception path without a listener."""
    transport = MemoryTransport()
    connection = h11.Connection(h11.SERVER)
    method = "GET" if body is None else "PUT"
    raw_body = b"" if body is None else json.dumps(body).encode()
    headers = [(b"host", b"synthetic"), (b"authorization", ("Bearer " + main.TOKEN).encode()),
               (b"content-type", b"application/json"), (b"content-length", str(len(raw_body)).encode())]
    raw_headers = b"\r\n".join(key+b": "+value for key, value in headers)
    connection.receive_data(method.encode()+b" "+path.encode()+b" HTTP/1.1\r\n"+raw_headers+b"\r\n\r\n"+raw_body)
    assert isinstance(connection.next_event(), h11.Request)
    if raw_body:
        assert isinstance(connection.next_event(), h11.Data)
    assert isinstance(connection.next_event(), h11.EndOfMessage)
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "scheme": "http", "method": method, "root_path": "", "path": path,
             "raw_path": path.encode(), "query_string": b"", "headers": headers,
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 2), "state": {}}
    event = asyncio.Event()
    event.set()
    cycle = RequestResponseCycle(scope, connection, transport, FlowControl(transport), logging.getLogger("uvicorn.error"),
                                 logging.getLogger("uvicorn.access"), False, [], event, lambda: None)
    cycle.body, cycle.more_body = raw_body, False
    await cycle.run_asgi(app)
    assert cycle.response_started and cycle.response_complete
    response = h11.Connection(h11.CLIENT)
    response.receive_data(b"".join(transport.writes))
    status, chunks = response.next_event(), []
    while isinstance(part := response.next_event(), h11.Data):
        chunks.append(part.data)
    assert isinstance(part, h11.EndOfMessage)
    return status.status_code, json.loads(b"".join(chunks))


@pytest.mark.parametrize("error_type", [RuntimeError, psycopg.errors.UntranslatableCharacter])
async def test_storage_error_and_cause_are_private_through_actual_uvicorn(error_type, monkeypatch, caplog):
    error, cause = error_type(PRIVATE), ValueError(CAUSE_PRIVATE)

    class FailedStore:
        async def call(self, *args, **kwargs):
            raise error from cause

    monkeypatch.setattr(main.app.state, "store", FailedStore(), raising=False)
    with caplog.at_level(logging.ERROR):
        status, response = await run_uvicorn_request(main.app, f"{main.BASE}/{uuid4()}")
    assert status == 500 and response["error"]["code"] == "INTERNAL_SERVER_ERROR"
    trace_id = response["error"]["traceId"]
    assert UUID(trace_id).version == 4
    assert PRIVATE not in json.dumps(response) and CAUSE_PRIVATE not in json.dumps(response)
    assert PRIVATE not in caplog.text and CAUSE_PRIVATE not in caplog.text
    assert f"type={error_type.__name__} traceId={trace_id}" in caplog.text
    records = [record for record in caplog.records if record.name == "uvicorn.error"]
    assert len(records) == 1 and error_type.__name__ in records[0].getMessage()
    assert all(record.exc_info is None and record.exc_text is None for record in caplog.records)


def test_asgi_filter_clears_cached_traceback_and_preserves_other_diagnostics(caplog):
    logger = logging.getLogger("uvicorn.error")
    before = len(logger.filters)
    install_error_handlers(main.app)
    assert len(logger.filters) == before
    error = RuntimeError(PRIVATE)
    record = logger.makeRecord(logger.name, logging.ERROR, __file__, 1, "Exception in ASGI application\n", (), (type(error), error, None))
    logging.Formatter().format(record)
    assert PRIVATE in record.exc_text
    record.stack_info = PRIVATE
    logger.handle(record)
    assert PRIVATE not in caplog.text
    assert record.exc_info is None and record.exc_text is None and record.stack_info is None
    assert record.getMessage() == record.message == "Exception in ASGI application: RuntimeError"
    diagnostic = logger.makeRecord(logger.name, logging.ERROR, __file__, 1, "ASGI callable returned without completing response.", (), None)
    logger.handle(diagnostic)
    assert diagnostic.getMessage() == "ASGI callable returned without completing response."
    assert diagnostic in caplog.records


@pytest.fixture
def isolated_postgres():
    """Opt-in PG tools create only a fresh synthetic Unix-socket cluster."""
    pg_bin = Path(os.environ["SYNTHETIC_CHAT_PG_BIN"])
    with tempfile.TemporaryDirectory(prefix="chat-privacy-", dir="/tmp") as temporary:
        root = Path(temporary)
        cluster, sockets, log = root / "cluster", root / "sockets", root / "postgres.log"
        sockets.mkdir()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "en_US.UTF-8"}

        def command(name, *args):
            result = subprocess.run([str(pg_bin / name), *args], env=env, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr

        command("initdb", "-D", str(cluster), "-U", "fixture_owner", "--auth-local=trust", "--auth-host=reject", "--no-locale", "-E", "UTF8")
        started = False
        try:
            command("pg_ctl", "-D", str(cluster), "-l", str(log), "-o", f"-h '' -k {sockets}", "-w", "start")
            started = True
            admin = f"postgresql://fixture_owner@/postgres?host={sockets}"
            with psycopg.connect(admin, autocommit=True) as db:
                db.execute("CREATE ROLE ai_chat_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
                db.execute("CREATE DATABASE emmet4930_chat_privacy")
            owner = admin.replace("/postgres?", "/emmet4930_chat_privacy?")
            with psycopg.connect(owner) as db:
                db.execute("CREATE SCHEMA ai_chat")
                db.execute("SET LOCAL search_path=ai_chat,public")
                db.execute((Path(__file__).resolve().parents[1] / "sql/state.sql").read_text())
                db.execute("GRANT USAGE ON SCHEMA ai_chat TO ai_chat_service")
                db.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA ai_chat TO ai_chat_service")
                db.execute("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA ai_chat TO ai_chat_service")
            url = owner.replace("fixture_owner@", "ai_chat_service@") + "&options=-csearch_path%3Dai_chat,public"
            yield url, log
        finally:
            if started:
                command("pg_ctl", "-D", str(cluster), "-m", "fast", "-w", "stop")


@pytest.mark.skipif(not os.getenv("SYNTHETIC_CHAT_PG_BIN"), reason="requires explicit PG tools for a new disposable cluster")
async def test_nul_input_never_reaches_postgres_or_any_error_log(isolated_postgres, monkeypatch, caplog):
    from app.local_store import LocalStore
    from app.mock_runtime import MockRuntime, NoExternalSearch

    url, postgres_log = isolated_postgres
    store = LocalStore(url)
    owner, cid = str(uuid4()), str(uuid4())
    assert store.store.lease(owner)
    store.store.conversation(cid, 71, 42)
    runtime = MockRuntime(store, NoExternalSearch(), owner)
    monkeypatch.setattr(main.app.state, "store", store, raising=False)
    monkeypatch.setattr(main.app.state, "runtime", runtime, raising=False)
    monkeypatch.setattr(main.app.state, "runtime_mode", "mock", raising=False)
    try:
        for field in ["message", "giftPreference", "categoryName"]:
            status, response = await run_uvicorn_request(main.app, f"{main.BASE}/{cid}/turns/{uuid4()}", body=nul_body(field))
            assert status == 400 and response["error"]["code"] == "INVALID_REQUEST"
            assert PRIVATE not in json.dumps(response)
        assert store.store.read(cid)["turns"] == [] and not runtime.jobs
        assert PRIVATE not in caplog.text
        assert PRIVATE not in postgres_log.read_text()
        assert not any(record.name == "uvicorn.error" for record in caplog.records)
    finally:
        await runtime.close()
        await store.close()
