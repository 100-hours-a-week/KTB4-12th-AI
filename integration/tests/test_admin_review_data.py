"""Administrative notes and trace persistence on isolated synthetic PostgreSQL.

These checks validate storage and access boundaries. They do not grade answers
or substitute automatic checks for the REVIEW-GUIDE's qualitative assessment.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
from threading import Barrier
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest

from integration.tests.test_single_server import database  # noqa: F401


pytestmark = pytest.mark.skipif(
    not os.getenv("SINGLE_SERVER_TEST_CLUSTER_URL"),
    reason="Run only through the isolated PostgreSQL scenario runner",
)


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    async def deny_async(*args, **kwargs):
        raise AssertionError("Real outbound HTTP is forbidden in admin data tests")

    def deny_sync(*args, **kwargs):
        raise AssertionError("Real outbound HTTP is forbidden in admin data tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_sync)


def chat_store(database):
    from app.store import Store
    return Store(database["chat"] + "?options=-csearch_path%3Dai_chat,public")


def conversation(store, *, status="COMPLETED", owner=None):
    owner, cid, tid = owner or str(uuid4()), str(uuid4()), str(uuid4())
    assert store.lease(owner)
    store.conversation(cid, 901, 902)
    store.start(cid, tid, "합성 일반 대화 질문", owner)
    store.emit(cid, tid, "answer.delta", {"delta": "합성 답변"}, owner)
    if status != "PROCESSING":
        ending = {"status": status}
        if status == "FAILED":
            ending["error"] = {"message": "합성 장애"}
        store.emit(cid, tid, "turn.finished", ending, owner)
    return cid, tid, owner


def note_rows(database):
    with psycopg.connect(database["owner"]) as db:
        return db.execute("SELECT id,turn,note,created FROM ai_chat.turn_reviews ORDER BY created,id").fetchall()


def test_admin_note_on_unreported_turn_is_durable_and_not_public(database):
    from app.turn_report import report
    store = chat_store(database)
    try:
        cid, tid, _ = conversation(store)
        review_id = str(uuid4())
        value = store.admin_turn_review(cid, tid, review_id, "  PRIVATE_ADMIN_NOTE_CANARY  ")
        assert value["conversationId"] == cid and value["turnId"] == tid
        assert value["reviewId"] == review_id and value["note"] == "PRIVATE_ADMIN_NOTE_CANARY"
        assert value["saved"] and not value["duplicate"]
        detail = store.admin_conversation(cid)
        assert detail["turns"][0]["reviews"] == [
            {"reviewId": review_id, "note": "PRIVATE_ADMIN_NOTE_CANARY", "created": value["created"]}]
        assert detail["turns"][0]["feedback"] == []
        assert store.admin_conversations()["items"][0]["feedbackCount"] == 0
        assert store.admin_reports()["reports"] == []
        assert store.own_feedback(901)["reports"] == []
        public = [store.read(cid), store.replay(cid, tid, 0), store.list_conversations(901),
                  report(cid, store.read(cid)["turns"][0], store.context(cid, tid))]
        assert "PRIVATE_ADMIN_NOTE_CANARY" not in json.dumps(public)
        assert "reviews" not in json.dumps(public)
        before = note_rows(database)
        store.close()
        store = chat_store(database)
        repeated = store.admin_turn_review(cid, tid, review_id, "PRIVATE_ADMIN_NOTE_CANARY")
        assert repeated["duplicate"] and repeated["created"] == value["created"]
        assert note_rows(database) == before
        assert store.admin_conversation(cid)["turns"][0]["reviews"] == detail["turns"][0]["reviews"]
    finally:
        store.close()


def test_review_id_cannot_change_note_turn_or_conversation(database):
    from app.errors import APIError
    store = chat_store(database)
    try:
        cid, tid, owner = conversation(store)
        other_cid, other_tid, _ = conversation(store, status="FAILED", owner=owner)
        ident = str(uuid4())
        store.admin_turn_review(cid, tid, ident, "첫 검토 메모")
        before = note_rows(database)
        for args, expected in [
            ((cid, tid, ident, "다른 검토 메모"), 409),
            ((other_cid, other_tid, ident, "첫 검토 메모"), 409),
            ((other_cid, tid, str(uuid4()), "잘못 묶은 메모"), 404),
            ((cid, str(uuid4()), str(uuid4()), "없는 질문 메모"), 404),
        ]:
            with pytest.raises(APIError) as rejected:
                store.admin_turn_review(*args)
            assert rejected.value.status == expected
            assert note_rows(database) == before
        second = store.admin_turn_review(cid, tid, str(uuid4()), "후속 검토도 이전 메모를 보존")
        assert not second["duplicate"] and len(note_rows(database)) == 2
        assert len(store.admin_conversation(cid)["turns"][0]["reviews"]) == 2
    finally:
        store.close()


def test_concurrent_same_review_id_is_one_append_and_conflict_is_not_overwrite(database):
    from app.errors import APIError
    store = chat_store(database)
    try:
        cid, tid, _ = conversation(store)
        ident, barrier = str(uuid4()), Barrier(4)

        def append():
            barrier.wait(timeout=10)
            return store.admin_turn_review(cid, tid, ident, "동일한 합성 검토")

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: append(), range(4)))
        assert sum(not value["duplicate"] for value in results) == 1
        assert len({value["created"] for value in results}) == 1
        assert len(note_rows(database)) == 1
        ident, barrier = str(uuid4()), Barrier(2)

        def conflict(note):
            barrier.wait(timeout=10)
            try:
                return store.admin_turn_review(cid, tid, ident, note)
            except APIError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(conflict, ["경합 메모 A", "경합 메모 B"]))
        assert sum(value == 409 for value in results) == 1
        assert len(note_rows(database)) == 2
    finally:
        store.close()


@pytest.mark.parametrize("note", ["", "   ", "x" * 1501, "NUL\x00본문", "고립\ud800본문"],
                         ids=["empty", "whitespace", "over_limit", "nul", "surrogate"])
def test_invalid_admin_note_cannot_write(database, note):
    from app.errors import APIError
    store = chat_store(database)
    try:
        cid, tid, _ = conversation(store)
        # The Store/RPC boundary also returns a request error before writing.
        with pytest.raises(APIError) as rejected:
            store.admin_turn_review(cid, tid, str(uuid4()), note)
        assert rejected.value.status == 400
        assert note_rows(database) == []
    finally:
        store.close()


def test_review_table_is_append_only_for_chat_and_inaccessible_to_other_roles(database):
    store = chat_store(database)
    try:
        cid, tid, _ = conversation(store)
        store.admin_turn_review(cid, tid, str(uuid4()), "권한 검사 메모")
        before = note_rows(database)
        with psycopg.connect(database["chat"]) as db:
            assert db.execute("SELECT count(*) FROM ai_chat.turn_reviews").fetchone()[0] == 1
        for statement in ["UPDATE ai_chat.turn_reviews SET note='changed'",
                          "DELETE FROM ai_chat.turn_reviews", "TRUNCATE ai_chat.turn_reviews"]:
            with psycopg.connect(database["chat"]) as db:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    db.execute(statement)
        for role in ("profiler", "search", "sync"):
            with psycopg.connect(database[role]) as db:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    db.execute("SELECT * FROM ai_chat.turn_reviews")
        with psycopg.connect(database["chat"]) as db:
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                db.execute("INSERT INTO ai_chat.turn_reviews(id,turn,note) VALUES(%s,%s,%s)",
                           (uuid4(), uuid4(), "없는 질문의 메모"))
        assert note_rows(database) == before
    finally:
        store.close()


def test_standalone_migration_also_keeps_review_notes_append_only(database):
    base = os.environ["SINGLE_SERVER_TEST_CLUSTER_URL"]
    name = "emmet4930_review_" + uuid4().hex[:12]
    with psycopg.connect(base, autocommit=True) as db:
        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname='ai_migrator'").fetchone():
            db.execute("CREATE ROLE ai_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        db.execute(sql.SQL("CREATE DATABASE {} OWNER ai_migrator").format(sql.Identifier(name)))
    url = base.rsplit("/", 1)[0].replace("fixture_owner@", "ai_migrator@") + "/" + name
    root = Path(__file__).resolve().parents[2]
    psql = Path(os.environ["SINGLE_SERVER_TEST_PG_CTL"]).with_name("psql")
    for _ in range(2):
        result = subprocess.run([str(psql), url, "-X", "-v", "ON_ERROR_STOP=1", "-v",
            "expected_database=" + name, "-f", str(root / "chat/sql/migrate.sql")],
            capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
    app_url = url.replace("ai_migrator@", "ai_chat_service@")
    cid, tid, review_id = uuid4(), uuid4(), uuid4()
    with psycopg.connect(app_url) as db:
        db.execute("INSERT INTO ai_chat.conversations(id,sender,recipient) VALUES(%s,1,2)", (cid,))
        db.execute("INSERT INTO ai_chat.turns(id,conversation,state) VALUES(%s,%s,%s)",
                   (tid, cid, Jsonb({"status": "COMPLETED"})))
        db.execute("INSERT INTO ai_chat.turn_reviews(id,turn,note) VALUES(%s,%s,'합성 검토')",
                   (review_id, tid))
        assert db.execute("SELECT note FROM ai_chat.turn_reviews WHERE id=%s", (review_id,)).fetchone()[0] == "합성 검토"
    for statement in ["UPDATE ai_chat.turn_reviews SET note='changed'", "DELETE FROM ai_chat.turn_reviews"]:
        with psycopg.connect(app_url) as db:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute(statement)


def database_contents(database):
    """Compare every old table and privilege, including the unchanged identity."""
    with psycopg.connect(database["owner"]) as db:
        tables = db.execute("""SELECT schemaname,tablename FROM pg_tables
            WHERE schemaname LIKE 'ai_%' OR (schemaname='public' AND tablename='alembic_version')
            ORDER BY 1,2""").fetchall()
        contents = {}
        for schema, table in tables:
            if (schema, table) == ("ai_chat", "turn_reviews"):
                continue
            rows = db.execute(sql.SQL("SELECT to_jsonb(t)::text FROM {}.{} t ORDER BY to_jsonb(t)::text")
                              .format(sql.Identifier(schema), sql.Identifier(table))).fetchall()
            contents[schema + "." + table] = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
        contents["grants"] = db.execute("""SELECT table_schema,table_name,grantee,privilege_type
            FROM information_schema.role_table_grants WHERE table_schema LIKE 'ai_%'
            AND NOT(table_schema='ai_chat' AND table_name='turn_reviews') ORDER BY 1,2,3,4""").fetchall()
    return contents


def test_additive_v1_reviews_preserve_all_existing_data_and_grants(database, monkeypatch):
    from integration.migrate_single_server import migrate
    store = chat_store(database)
    try:
        cid, tid, owner = conversation(store)
        store.feedback(cid, tid, str(uuid4()), {"verdict": "helpful", "note": "기존 사용자 의견"})
        store.conversation_feedback(cid, str(uuid4()), {"category": "quality", "note": "기존 대화 신고"})
        assert store.lease(owner, release=True)
        store.close()
        with psycopg.connect(database["owner"]) as db:
            # Reproduce the original /1 shape without the additive review table.
            # while keeping all preexisting rows, including legacy trace absence.
            db.execute("DROP TABLE ai_chat.turn_reviews")
            db.execute("UPDATE ai_chat.turns SET context=%s WHERE id=%s",
                       (Jsonb({"legacyTrace": "PRESERVE_WITHOUT_INVENTING_STATUS"}), tid))
            db.execute("""INSERT INTO ai_profile.read_grants
                (id,caller_user_id,recipient_user_id,source_version,expires_at)
                VALUES (%s,901,902,1,now()+interval '1 hour')""", (uuid4(),))
        before = database_contents(database)
        with pytest.raises(RuntimeError, match="Chat review migration required"):
            chat_store(database)
        assert database_contents(database) == before
        monkeypatch.setenv("INTEGRATION_SYNTHETIC_ONLY", "1")
        result = migrate(database["owner"], database["name"])
        assert result["changed"] and result["version"] == "single-server/1"
        assert database_contents(database) == before
        assert not migrate(database["owner"], database["name"])["changed"]
        assert database_contents(database) == before
        store = chat_store(database)
        assert store.context(cid, tid) == {"legacyTrace": "PRESERVE_WITHOUT_INVENTING_STATUS"}
        assert store.admin_conversation(cid)["turns"][0]["reviews"] == []
        assert store.admin_turn_review(cid, tid, str(uuid4()), "업그레이드 후 첫 검토")["saved"]
    finally:
        store.close()


@pytest.mark.parametrize("damage", ["column", "select_grant"])
def test_review_migration_preflight_checks_columns_and_grants_and_closes_pool(database, monkeypatch, damage):
    import app.store as store_module
    pools = []
    original = store_module.ConnectionPool

    def pool(*args, **kwargs):
        value = original(*args, **kwargs)
        pools.append(value)
        return value

    with psycopg.connect(database["owner"]) as db:
        db.execute("ALTER TABLE ai_chat.turn_reviews DROP COLUMN note" if damage == "column"
                   else "REVOKE SELECT ON ai_chat.turn_reviews FROM ai_chat_service")
    monkeypatch.setattr(store_module, "ConnectionPool", pool)
    with pytest.raises(RuntimeError, match="Chat review migration required"):
        chat_store(database)
    assert len(pools) == 1 and pools[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "before_write", "lost_ack", "interrupted"])
async def test_trace_status_follows_durable_write_even_when_acknowledgement_is_lost(database, monkeypatch, mode):
    from app.runtime import Runtime
    store = chat_store(database)
    writes = []
    secret = "synthetic-private-provider-key-canary"
    monkeypatch.setenv("SYNTHETIC_PROVIDER_API_KEY", secret)

    class Adapter:
        async def call(self, method, **args):
            if method == "context" and args.get("value") is not None:
                writes.append(args["value"])
                if mode == "before_write":
                    raise RuntimeError("synthetic persistence failure")
                result = store.context(**args)
                if mode == "lost_ack":
                    raise RuntimeError("synthetic acknowledgement lost after commit")
                return result
            return getattr(store, method)(**args)

    try:
        cid, tid, owner = conversation(store, status="PROCESSING")
        pending = store.context(cid, tid)
        assert pending["tracePersistence"] == {"version": 1, "status": "pending"}
        runtime = Runtime.__new__(Runtime)
        runtime.init_state(Adapter(), None, owner)
        receipt = {"outcome": "INTERRUPTED" if mode == "interrupted" else "COMPLETED",
                   "model": "synthetic", "operation": "synthetic evidence",
                   "authorization": "Bearer " + secret, "providerNote": secret}
        saved = await runtime.save_context(cid, tid, receipt)
        assert saved is (mode in {"success", "interrupted"})
        assert len(writes) == (2 if mode in {"before_write", "lost_ack"} else 1)
        assert secret not in json.dumps(writes)
        if mode == "interrupted":
            # Recovery uses _event directly rather than Runtime.finish.
            assert store.lease(owner, recover=True)
            assert store.read(cid)["turns"][0]["status"] == "FAILED"
        else:
            assert await runtime.finish(cid, tid, {"status": "COMPLETED"})
            assert store.read(cid)["turns"][0]["status"] == "COMPLETED"
        actual = store.context(cid, tid)
        expected = {"success": "recorded", "before_write": "not_recorded",
                    "lost_ack": "recorded", "interrupted": "interrupted"}[mode]
        assert actual["tracePersistence"] == {"version": 1, "status": expected}
        # Admission owns this snapshot; trace persistence must not replace it,
        # including when the trace write failed before reaching the database.
        assert actual["recipientProfile"] == pending["recipientProfile"]
        if mode == "before_write":
            assert "operation" not in actual
        else:
            assert actual["operation"] == "synthetic evidence"
        public = json.dumps([store.read(cid), store.replay(cid, tid, 0)])
        assert "tracePersistence" not in public and "synthetic evidence" not in public and secret not in public
        store.close()
        store = chat_store(database)
        assert store.context(cid, tid) == actual
    finally:
        store.close()


def test_recovery_marks_only_pending_trace_missing_and_preserves_legacy_unknown(database):
    store = chat_store(database)
    try:
        cid, tid, owner = conversation(store, status="PROCESSING")
        legacy_cid, legacy_tid, _ = conversation(store, status="PROCESSING", owner=owner)
        with psycopg.connect(database["owner"]) as db:
            db.execute("UPDATE ai_chat.turns SET context=%s WHERE id=%s",
                       (Jsonb({"legacy": "trace status was never recorded"}), legacy_tid))
        assert store.lease(owner, recover=True)
        assert store.context(cid, tid)["tracePersistence"] == {"version": 1, "status": "not_recorded"}
        assert store.context(legacy_cid, legacy_tid) == {"legacy": "trace status was never recorded"}
        for conversation_id, turn_id in [(cid, tid), (legacy_cid, legacy_tid)]:
            events, state = store.replay(conversation_id, turn_id, 0)
            assert state["status"] == "FAILED" and events[-1]["event"] == "turn.finished"
            assert events[-1]["data"] == state
            assert "tracePersistence" not in json.dumps(events)
    finally:
        store.close()
