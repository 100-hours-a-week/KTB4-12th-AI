"""Synthetic model checks cannot bypass existing current-version profile grants."""
import asyncio
import copy
import json
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest

from app import main
from app.errors import APIError
from app.runtime import Runtime
from app.store import Store


EMPTY = {'recipientUserId': 42, 'status': 'NONE', 'sourceVersion': 0,
         'giftPreference': None, 'dislikedCategories': []}
DENIED = {'source': 'AI_PROFILE_STORE', 'status': 'UNAVAILABLE',
          'accessStatus': 'NOT_AUTHORIZED'}


class Database:
    """Only the authorized boolean may leave the synthetic SQL projection."""
    def __init__(self, authorized, old=None, history=False):
        self.authorized, self.old, self.history = authorized, old, history
        self.calls = []
        self.query = ''

    def execute(self, query, args=None):
        self.query = query
        self.calls.append((query, args))
        return self

    def fetchone(self):
        if self.query.startswith('SELECT * FROM turns'): return self.old
        if 'LIMIT 1' in self.query: return {'exists': 1} if self.history else None
        if ' AS authorized' in self.query:
            assert 'ai_profile.chat_recipient_snapshot(%s)' in self.query
            assert "jsonb_typeof(snapshot->'inputSourceVersion')='number'" in self.query
            assert "IS DISTINCT FROM 'NOT_AUTHORIZED'" in self.query
            return {'authorized': self.authorized}
        if self.query == 'SELECT ai_profile.chat_recipient_snapshot(%s) AS snapshot':
            # The actual SQL grant/expiry/version projection is exercised by
            # integration/tests/test_recipient_data.py, not this adapter.
            assert self.authorized is False
            return {'snapshot': copy.deepcopy(DENIED)}
        if 'count(*)' in self.query: return {'n': 0}
        if 'FROM admission_control' in self.query: return {'paused': False}
        if self.query.startswith('SELECT 1 FROM turns'): return None
        raise AssertionError(self.query)


def store_fixture(authorized, *, lookup=True, old=None, history=False):
    db = Database(authorized, old, history)
    class Pool:
        @contextmanager
        def connection(self): yield db
    store = Store.__new__(Store)
    store.pool, store.profile_lookup = Pool(), lookup
    store._guard = lambda *args: None
    store._conversation = lambda *args: {'recipient': 42}
    return store, db


@pytest.mark.parametrize('authorized,lookup', [(False, True), (None, True), (True, False), (True, True)])
def test_new_check_requires_current_grant_before_insert_without_reading_private_fields(authorized, lookup):
    store, db = store_fixture(authorized, lookup=lookup)
    args = dict(cid='c', tid='t', message='synthetic', owner='owner', recipient_context=EMPTY, quick_check=True)
    if authorized is True and lookup:
        result, made = store.start(**args)
        assert made and result['status'] == 'PROCESSING'
        inserted = next(args for query, args in db.calls if query.startswith('INSERT INTO turns'))
        # The granted input version can be any current version. It is not
        # replaced by the diagnostic NONE/version-zero payload or persisted raw.
        assert inserted[4].obj == EMPTY
        assert inserted[-1].obj == {'tracePersistence': {'version': 1, 'status': 'pending'}}
    else:
        with pytest.raises(APIError) as error: store.start(**args)
        assert error.value.status == 409 and error.value.message == 'QUICK_CHECK_GRANT_REQUIRED'
        assert not any(query.startswith(('INSERT', 'UPDATE', 'DELETE')) for query, _ in db.calls)
    assert not any('FROM ai_profile.read_grants' in query for query, _ in db.calls)


@pytest.mark.parametrize('status', ['PROCESSING', 'COMPLETED', 'FAILED', 'CANCELLED'])
def test_existing_check_remains_a_read_after_grant_revocation(status):
    old = {'conversation': 'c', 'state': {'message': 'synthetic', 'status': status},
        'requested_model': None, 'recipient_context': EMPTY, 'prepared_context': None}
    store, db = store_fixture(False, old=old)
    assert store.start('c', 't', 'synthetic', 'owner', recipient_context=EMPTY, quick_check=True) == [old['state'], False]
    assert not any('chat_recipient_snapshot' in query or query.startswith(('INSERT', 'UPDATE')) for query, _ in db.calls)


@pytest.mark.parametrize('context,history,code', [
    (None, False, 'QUICK_CHECK_SYNTHETIC_CONTEXT_REQUIRED'),
    (EMPTY | {'status': 'AVAILABLE', 'sourceVersion': 8, 'giftPreference': 'synthetic private text'}, False, 'QUICK_CHECK_SYNTHETIC_CONTEXT_REQUIRED'),
    (EMPTY, True, 'QUICK_CHECK_NEW_CONVERSATION_REQUIRED'),
])
def test_check_cannot_send_private_context_or_inherit_existing_conversation(context, history, code):
    store, db = store_fixture(True, history=history)
    with pytest.raises(APIError) as error:
        store.start('c', 't', 'synthetic', 'owner', recipient_context=context, quick_check=True)
    assert error.value.message == code
    assert not any(query.startswith(('INSERT', 'UPDATE')) for query, _ in db.calls)


@pytest.mark.parametrize('flag,authorized,expected', [(True, False, 409), (True, True, 202), (False, False, 202)])
async def test_http_header_reaches_transaction_and_denial_never_starts_generation(monkeypatch, flag, authorized, expected):
    store, db = store_fixture(authorized)
    generated = []
    class Adapter:
        async def call(self, method, **args):
            if method == 'binding': return {'protocolFixture': False}
            if method == 'existing_turn': return None
            if method == 'start':
                assert args.get('quick_check', False) is flag
                return store.start(**args)
            raise AssertionError(method)
    adapter = Adapter()
    runtime = Runtime.__new__(Runtime); runtime.init_state(adapter, None, 'owner'); runtime.model = 'synthetic/model'
    async def run(cid, tid): generated.append(tid)
    runtime.run = run
    monkeypatch.setattr(main.app.state, 'store', adapter, raising=False)
    monkeypatch.setattr(main.app.state, 'runtime', runtime, raising=False)
    monkeypatch.setattr(main.app.state, 'runtime_mode', 'live', raising=False)
    cid, tid = str(uuid4()), str(uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(main.app), base_url='https://synthetic.invalid',
        headers={'Authorization': 'Bearer ' + main.TOKEN}) as client:
        result = await client.put(f'{main.BASE}/{cid}/turns/{tid}', json={
            'message': 'synthetic', 'recipientContext': EMPTY, 'quickCheck': flag},
            headers={'X-Console-Quick-Check': 'true'} if flag else {})
    await asyncio.gather(*list(runtime.jobs.values()))
    assert result.status_code == expected
    assert len(generated) == int(expected == 202)
    if expected == 409:
        assert result.json()['message'] == 'QUICK_CHECK_GRANT_REQUIRED'
        assert not any(query.startswith(('INSERT', 'UPDATE')) for query, _ in db.calls)
    if not flag:
        assert not any('chat_recipient_snapshot' in query for query, _ in db.calls)


@pytest.mark.parametrize('header,marker,expected', [
    ('true', {}, 400),
    (None, {'quickCheck': True}, 400),
    ('false', {'quickCheck': True}, 400),
    ('TRUE', {'quickCheck': True}, 400),
    ('true', {'quickCheck': False}, 400),
    # Chat deliberately normalizes schema failures to its private HTTP 400
    # envelope rather than exposing FastAPI's default 422/input detail.
    ('true', {'quickCheck': 'true'}, 400),
    ('true', {'quickCheck': 1}, 400),
    ('true', {'quickCheck': None}, 400),
], ids=['header-only', 'body-only', 'false-header', 'uppercase-header',
        'false-body', 'string-body', 'integer-body', 'null-body'])
async def test_incomplete_or_invalid_check_markers_never_reach_admission(monkeypatch, header, marker, expected):
    from starlette.datastructures import State

    store, db = store_fixture(True)
    generated = []

    class Adapter:
        async def call(self, method, **args):
            if method == 'binding': return {'protocolFixture': False}
            if method == 'existing_turn': return None
            if method == 'start': return store.start(**args)
            raise AssertionError(method)

    adapter = Adapter()
    runtime = Runtime.__new__(Runtime)
    runtime.init_state(adapter, None, 'owner')
    runtime.model = 'synthetic/model'
    async def run(cid, tid): generated.append(tid)
    runtime.run = run
    monkeypatch.setattr(main.app, 'state', State({'store': adapter, 'runtime': runtime,
                                                'runtime_mode': 'live'}))
    headers = {'Authorization': 'Bearer ' + main.TOKEN}
    if header is not None:
        headers['X-Console-Quick-Check'] = header
    async with httpx.AsyncClient(transport=httpx.ASGITransport(main.app),
                                 base_url='https://synthetic.invalid') as client:
        result = await client.put(f'{main.BASE}/{uuid4()}/turns/{uuid4()}', headers=headers,
            json={'message': 'synthetic', 'recipientContext': EMPTY, **marker})
    await asyncio.gather(*list(runtime.jobs.values()))
    assert result.status_code == expected, result.text
    assert not db.calls and not generated


@pytest.mark.parametrize('marker', [None, False, True],
                         ids=['default', 'explicit-false', 'explicit-true'])
async def test_demo_proxy_omits_false_marker_and_preserves_explicit_check(monkeypatch, marker):
    from starlette.datastructures import State

    calls, forwarded = [], []
    cid, tid, sender = uuid4(), uuid4(), 7

    class DemoStore:
        async def call(self, method, **args):
            calls.append((method, args))
            if method == 'owned': return None
            if method == 'binding': return {'protocolFixture': False}
            raise AssertionError(method)

    async def capture(path, body):
        forwarded.append((path, body))
        return {'synthetic': True}

    monkeypatch.setattr(main.app, 'state', State({'store': DemoStore()}))
    monkeypatch.setattr(main, 'throttle', lambda *args: None)
    monkeypatch.setattr(main, 'proxied', capture)
    payload = {'message': 'synthetic'}
    if marker is not None:
        payload['quickCheck'] = marker
    result = await main.demo_turn(cid, tid, main.PutTurn.model_validate(payload), sender)

    assert result == {'synthetic': True}
    assert calls == [('owned', {'cid': str(cid), 'sender': sender}),
                     ('binding', {'cid': str(cid)})]
    # Ordinary demo traffic remains accepted by older strict schemas; an
    # explicit check must never be silently downgraded into an ordinary turn.
    expected = {'message': 'synthetic'} | ({'quickCheck': True} if marker else {})
    assert forwarded == [(f'{main.BASE}/{cid}/turns/{tid}', expected)]


@pytest.mark.parametrize('explicit_none', [False, True])
@pytest.mark.parametrize('lookup', [False, True])
async def test_ordinary_be_chat_without_grant_completes_public_recommendation(
        monkeypatch, explicit_none, lookup):
    """Normal BE admission still reaches the real harness with no private grant.

    Exercise HTTP auth, the real Store.start branch and the actual gift tool
    loop. Only SQL rows, product data and provider decisions are synthetic.
    """
    from starlette.datastructures import State
    from app.low_latency import LowLatencyRuntime
    from app.model_config import ModelCatalog
    from test_low_latency import Search, Store as ScriptedStore, context, show, tool

    sql_store, db = store_fixture(False, lookup=lookup)

    class OrdinaryStore(ScriptedStore):
        async def call(self, method, **args):
            if method != 'start':
                return await super().call(method, **args)
            assert not args.get('quick_check', False)
            _, made = sql_store.start(**args)
            assert made
            inserted = next(values for query, values in db.calls if query.startswith('INSERT INTO turns'))
            self.supplied = copy.deepcopy(inserted[4].obj) if inserted[4] is not None else None
            result = await super().call(method, **args)
            self.traces[args['tid']] = copy.deepcopy(inserted[-1].obj)
            return result

    store, search, wire = OrdinaryStore(), Search(), []
    packets = [tool('gift_context', context(budget=20000)), tool('show_gifts', show())]

    def provider(request):
        assert request.method == 'POST' and request.url.path == '/api/v1/chat/completions'
        wire.append(json.loads(request.content))
        return httpx.Response(200, text='data: ' + json.dumps(packets.pop(0), ensure_ascii=False)
                              + '\n\ndata: [DONE]\n\n')

    async with httpx.AsyncClient(base_url='https://openrouter.ai/api/v1',
                                transport=httpx.MockTransport(provider)) as http:
        runtime = LowLatencyRuntime.__new__(LowLatencyRuntime)
        runtime.init_state(store, search, 'owner')
        runtime.model = 'openai/gpt-6-luna'
        runtime.http = http
        runtime.catalog = ModelCatalog(http)
        monkeypatch.setattr(main.app, 'state', State({'store': store, 'runtime': runtime,
                                                    'runtime_mode': 'live'}))
        cid, tid = str(uuid4()), str(uuid4())
        path = main.BASE + '/' + cid
        auth = {'Authorization': 'Bearer ' + main.TOKEN}
        body = {'message': '2만원 이하 책받침 선물 하나 추천해줘'}
        if explicit_none:
            body['recipientContext'] = EMPTY
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(main.app),
                                         base_url='https://synthetic.invalid') as client:
                assert (await client.put(path, headers=auth,
                    json={'senderUserId': 7, 'recipientUserId': 42})).status_code == 201
                # No private-data grant is required; BE service authentication is.
                assert (await client.put(path + '/turns/' + tid, json=body)).status_code == 401
                assert not db.calls and not wire
                accepted = await client.put(path + '/turns/' + tid, headers=auth, json=body)
                assert accepted.status_code == 202, accepted.text
                await asyncio.gather(*list(runtime.jobs.values()))
                final = await client.get(path + '/turns/' + tid, headers=auth)
                assert final.status_code == 200 and final.json()['data']['status'] == 'COMPLETED'
        finally:
            await runtime.stop_jobs()

    assert len(wire) == 2 and not packets
    assert [query['query'] for query in search.queries] == ['책받침']
    assert search.queries[0]['filters']['excludeCategoryIds'] == []
    assert [product['productId'] for product in store.traces[tid]['selectedProducts']] == [101]
    snapshot = store.traces[tid]['recipientProfile']
    expected = ({'source': 'BACKEND_REQUEST', 'status': 'NONE', 'data': EMPTY} if explicit_none
                else DENIED if lookup else {'status': 'NOT_PROVIDED'})
    assert snapshot == expected
    if not explicit_none:
        assert 'data' not in snapshot
    assert not any(' AS authorized' in query for query, _ in db.calls)
    assert sum('chat_recipient_snapshot' in query for query, _ in db.calls) == int(lookup and not explicit_none)
