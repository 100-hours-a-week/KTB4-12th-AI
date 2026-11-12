"""Only active-count quotas are removed; admission identity and daily quota remain."""
from contextlib import nullcontext
from uuid import uuid4

import pytest

from app.errors import APIError
from app.store import Store


class Rows:
    def __init__(self, value=None):
        self.value = value

    def fetchone(self):
        return self.value


class DB:
    def __init__(self):
        self.turns = {}
        self.statements = []

    def execute(self, sql, args=()):
        self.statements.append(sql)
        if sql.startswith('SELECT * FROM turns WHERE id='):
            return Rows(self.turns.get(args[0]))
        if sql.startswith('SELECT 1 FROM turns WHERE conversation='):
            return Rows(next((t for t in self.turns.values()
                              if t['conversation'] == args[0]), None))
        if "count(*) AS n FROM turns WHERE created>" in sql:
            return Rows({'n': len(self.turns)})
        if sql.startswith('SELECT count(*)'):
            # This was the removed active-generation query. Old code would
            # reject the fifth turn even with a large daily quota.
            return Rows({'n': len(self.turns)})
        if sql.startswith('SELECT paused'):
            return Rows({'paused': False})
        if sql.startswith('INSERT INTO turns'):
            tid, cid, state, model, recipient, prepared, context = args
            self.turns[tid] = dict(conversation=cid, state=state.obj,
                                  requested_model=model, recipient_context=None,
                                  prepared_context=None)
        return Rows()


def fake_store():
    db = DB()
    store = object.__new__(Store)
    store.pool = type('Pool', (), {'connection': lambda self: nullcontext(db)})()
    store._guard = lambda *args: None
    store._conversation = lambda *args: {'recipient': 2}
    store.profile_lookup = False
    return store, db


def test_more_than_four_distinct_conversations_can_generate(monkeypatch):
    monkeypatch.setenv('CHAT_MAX_ACTIVE', '1')  # Legacy deployment value is ignored.
    monkeypatch.setenv('CHAT_DAILY_LIMIT', '200')
    store, db = fake_store()
    for _ in range(40):
        state, made = store.start(str(uuid4()), str(uuid4()), 'synthetic', 'owner')
        assert made and state['status'] == 'PROCESSING'
    assert len(db.turns) == 40
    assert not any("count(*) AS n FROM turns WHERE state" in sql for sql in db.statements)


def test_same_conversation_order_and_idempotency_still_apply():
    store, db = fake_store()
    cid, tid = str(uuid4()), str(uuid4())
    first, made = store.start(cid, tid, 'synthetic', 'owner')
    assert made
    assert store.start(cid, tid, 'synthetic', 'owner') == [first, False]
    with pytest.raises(APIError) as error:
        store.start(cid, str(uuid4()), 'another synthetic', 'owner')
    assert error.value.status == 409
    assert len(db.turns) == 1


def test_daily_quota_is_not_removed(monkeypatch):
    monkeypatch.setenv('CHAT_DAILY_LIMIT', '1')
    store, db = fake_store()
    store.start(str(uuid4()), str(uuid4()), 'synthetic', 'owner')
    with pytest.raises(APIError) as error:
        store.start(str(uuid4()), str(uuid4()), 'synthetic', 'owner')
    assert error.value.status == 429
    assert len(db.turns) == 1
