"""Stored retries and admission races must not depend on the model provider."""
import asyncio
import copy
import os
import time
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest

os.environ.setdefault('AI_SERVICE_TOKEN', 'test-service-token')
os.environ.setdefault('SESSION_SECRET', 'test-session-secret')
os.environ.setdefault('QA_ADMIN_PASSWORD', 'test-admin')
from app import main
from app.errors import APIError
from app.model_config import DEFAULT_MODEL, ModelCatalog
from app.runtime import Runtime
from app.store import Store


class SavedStore:
    def __init__(self, cid, tid, model=None, context=None, status='COMPLETED'):
        self.cid, self.tid, self.model, self.context = cid, tid, model, context
        self.state = {'turnId': tid, 'status': status, 'message': 'original',
                      'blocks': [], 'lastEventId': 1}
        self.calls = []

    async def call(self, method, **args):
        if method=='binding':return {'protocolFixture':False}
        self.calls.append(method)
        assert method == 'existing_turn', 'Stored retry attempted generation or another lookup'
        if (args['cid'], args['tid'], args['message'], args.get('model'), args.get('recipient_context')) != (self.cid, self.tid, 'original', self.model, self.context):
            raise APIError(409, 'request identity changed')
        return copy.deepcopy(self.state)


@pytest.mark.parametrize('provider_status', [503, 404])
@pytest.mark.parametrize('model', [None, DEFAULT_MODEL, 'vendor/removed-model'])
@pytest.mark.parametrize('entrypoint', ['runtime', 'http'])
async def test_terminal_retry_uses_store_with_expired_cache_and_unavailable_provider(monkeypatch, provider_status, model, entrypoint):
    cid, tid = str(uuid4()), str(uuid4())
    store = SavedStore(cid, tid, model)
    provider_calls = []
    def provider(request):
        provider_calls.append(request)
        return httpx.Response(provider_status)
    async with httpx.AsyncClient(base_url='https://synthetic.invalid', transport=httpx.MockTransport(provider)) as provider_http:
        runtime = Runtime.__new__(Runtime)
        runtime.init_state(store, None, 'fixture')
        runtime.model = DEFAULT_MODEL
        runtime.catalog = ModelCatalog(provider_http)
        runtime.catalog.cache = {key: (time.monotonic() - 7200, value) for key, (_, value) in runtime.catalog.cache.items()}
        runtime.healthy = False  # Stored reads also survive paused generation.
        if entrypoint == 'runtime':
            result = await runtime.accept(cid, tid, 'original', model=model)
        else:
            monkeypatch.setattr(main.app.state, 'runtime', runtime, raising=False)
            monkeypatch.setattr(main.app.state, 'store', store, raising=False)
            monkeypatch.setattr(main.app.state, 'runtime_mode', 'live', raising=False)
            body = {'message': 'original', **({'model': model} if model else {})}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://fixture', headers={'Authorization': 'Bearer ' + main.TOKEN}) as client:
                response = await client.put(f'{main.BASE}/{cid}/turns/{tid}', json=body)
            assert response.status_code == 200
            result = response.json()['data']
        assert result == store.state
        assert provider_calls == [] and store.calls == ['existing_turn']
        await asyncio.gather(*list(runtime.jobs.values()))


@pytest.mark.parametrize('changes', [{'message': 'changed'}, {'model': DEFAULT_MODEL},
                                    {'recipient_context': {'recipientUserId': 42, 'status': 'NONE', 'sourceVersion': 1, 'giftPreference': None, 'dislikedCategories': []}}])
async def test_changed_retry_conflicts_before_provider(changes):
    cid, tid = str(uuid4()), str(uuid4())
    runtime = Runtime.__new__(Runtime)
    runtime.init_state(SavedStore(cid, tid), None, 'fixture')
    runtime.model = DEFAULT_MODEL
    class ForbiddenCatalog:
        async def resolve(self, model): raise AssertionError('Provider resolution for existing turn')
    runtime.catalog = ForbiddenCatalog()
    with pytest.raises(APIError) as error:
        await runtime.accept(cid, tid, **{'message': 'original', **changes})
    assert error.value.status == 409 and runtime.healthy


async def test_new_concurrent_duplicates_share_metadata_and_generation():
    metadata_entered, metadata_release, generation_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = {'metadata': 0, 'start': 0, 'generate': 0}
    state = {'status': 'PROCESSING', 'lastEventId': 0}
    class NewStore:
        async def call(self, method, **args):
            if method == 'existing_turn': return None
            if method == 'start': calls['start'] += 1; return [state.copy(), True]
            if method == 'replay': return [[], state.copy()]
            raise AssertionError(method)
    class Catalog:
        async def resolve(self, model):
            calls['metadata'] += 1; metadata_entered.set(); await metadata_release.wait()
    runtime = Runtime.__new__(Runtime)
    runtime.init_state(NewStore(), None, 'fixture'); runtime.model = DEFAULT_MODEL; runtime.catalog = Catalog()
    async def run(cid, tid): calls['generate'] += 1; await generation_release.wait()
    runtime.run = run
    first = asyncio.create_task(runtime.accept('c', 't', 'original'))
    await metadata_entered.wait()
    duplicate = asyncio.create_task(runtime.accept('c', 't', 'original'))
    with pytest.raises(APIError) as conflict: await runtime.accept('c', 't', 'changed')
    assert conflict.value.status == 409
    metadata_release.set()
    assert await first == await duplicate == state
    assert calls == {'metadata': 1, 'start': 1, 'generate': 1}
    generation_release.set(); await asyncio.gather(*list(runtime.jobs.values()))


async def test_new_provider_failure_never_admits_or_pauses_storage():
    class NewStore:
        async def call(self, method, **args): assert method == 'existing_turn'; return None
    class Catalog:
        async def resolve(self, model): raise APIError(503, 'synthetic provider outage')
    runtime = Runtime.__new__(Runtime); runtime.init_state(NewStore(), None, 'fixture')
    runtime.model = DEFAULT_MODEL; runtime.catalog = Catalog()
    with pytest.raises(APIError): await runtime.accept('c', 't', 'new question')
    assert runtime.healthy


@pytest.mark.parametrize('changes', [{}, {'model': DEFAULT_MODEL}, {'message': 'changed'}, {'cid': 'other'}])
def test_storage_lookup_compares_persisted_identity_without_generation_lease(changes):
    old = {'conversation': 'c', 'requested_model': None, 'recipient_context': None, 'prepared_context':None,
           'state': {'status': 'COMPLETED', 'message': 'original'}}
    class Database:
        def execute(self, query, args): assert query.startswith('SELECT * FROM turns'); return self
        def fetchone(self): return old
    class Pool:
        @contextmanager
        def connection(self): yield Database()
    store = Store.__new__(Store); store.pool = Pool()
    store._conversation = lambda db, cid: {'recipient': 42}
    store._guard = lambda *args: pytest.fail('Read-only lookup required a generation lease')
    args = {'cid': 'c', 'tid': 't', 'message': 'original', **changes}
    if changes:
        with pytest.raises(APIError) as error: store.existing_turn(**args)
        assert error.value.status == 409
    else: assert store.existing_turn(**args) == old['state']
