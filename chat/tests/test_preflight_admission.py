"""Offline burst and worker isolation checks; all payloads are synthetic."""
import asyncio
import threading
import uuid

import httpx
import pytest
from app import main
from app.errors import APIError
from app.local_store import LocalStore
from app.protocol_fixture import ProtocolFixture
from app.resource_limits import ResourceLimits
from app.runtime import Runtime


async def eventually(check):
    async with asyncio.timeout(2):
        while not check():
            await asyncio.sleep(.001)


class BlockedReads:
    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()

    async def call(self, method, **args):
        self.calls.append(method)
        await self.release.wait()
        raise APIError(404, 'synthetic missing conversation')


def runtime_for(store):
    runtime = Runtime.__new__(Runtime)
    runtime.init_state(store, None, 'synthetic-owner')
    return runtime


async def test_direct_accept_bounds_first_read_and_retains_cancelled_capacity():
    store = BlockedReads()
    runtime = runtime_for(store)
    calls = [asyncio.create_task(runtime.accept(str(uuid.uuid4()), str(uuid.uuid4()), 'synthetic')) for _ in range(100)]
    await eventually(lambda: len(store.calls) == 8 and sum(t.done() for t in calls) == 92)
    assert store.calls == ['existing_turn'] * 8
    assert not runtime.jobs and not runtime.admissions
    for call in calls:
        call.cancel()
    results = await asyncio.gather(*calls, return_exceptions=True)
    assert sum(isinstance(r, APIError) and r.status == 503 for r in results) == 92
    assert runtime.resource_limits.preflight_reads == 8
    with pytest.raises(APIError) as rejected:
        await runtime.accept('synthetic-cid', 'synthetic-tid', 'synthetic')
    assert rejected.value.status == 503 and len(store.calls) == 8
    store.release.set()
    await eventually(lambda: runtime.resource_limits.preflight_reads == 0)


@pytest.mark.parametrize('base', [main.BASE, main.TEST_BASE])
async def test_http_rejects_before_binding_read(monkeypatch, base):
    store = BlockedReads()
    runtime = runtime_for(store)
    monkeypatch.setattr(main.app.state, 'store', store, raising=False)
    monkeypatch.setattr(main.app.state, 'runtime', runtime, raising=False)
    monkeypatch.setattr(main.app.state, 'fixture', ProtocolFixture(runtime), raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://test',
                                headers={'Authorization': 'Bearer ' + main.TOKEN}) as client:
        calls = [asyncio.create_task(client.put(f'{base}/{uuid.uuid4()}/turns/{uuid.uuid4()}', json={'message': 'synthetic'})) for _ in range(100)]
        await eventually(lambda: len(store.calls) == 8 and sum(t.done() for t in calls) == 92)
        assert store.calls == ['binding'] * 8
        assert all(t.result().status_code == 503 and t.result().headers['retry-after'] == '2' for t in calls if t.done())
        store.release.set()
        await asyncio.gather(*calls)
    assert runtime.resource_limits.preflight_reads == 0


async def test_demo_session_read_is_already_bounded(monkeypatch):
    store = BlockedReads()
    runtime = runtime_for(store)
    monkeypatch.setattr(main.app.state, 'store', store, raising=False)
    monkeypatch.setattr(main.app.state, 'runtime', runtime, raising=False)
    monkeypatch.setattr(main, 'same_origin', lambda request: None)
    monkeypatch.setattr(main, 'require_demo_access', lambda request: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://test',
                                cookies={'chat_session': main.SIGNER.dumps('synthetic-session', salt='demo')}) as client:
        calls = [asyncio.create_task(client.put(f'/demo/conversations/{uuid.uuid4()}/turns/{uuid.uuid4()}', json={'message': 'synthetic'})) for _ in range(100)]
        await eventually(lambda: len(store.calls) == 8 and sum(t.done() for t in calls) == 92)
        assert store.calls == ['session'] * 8
        assert all(t.result().status_code == 503 for t in calls if t.done())
        store.release.set()
        await asyncio.gather(*calls)


async def test_demo_ownership_read_is_bounded_even_without_session_dependency(monkeypatch):
    store = BlockedReads()
    runtime = runtime_for(store)
    monkeypatch.setattr(main.app.state, 'store', store, raising=False)
    monkeypatch.setattr(main.app.state, 'runtime', runtime, raising=False)
    calls = [asyncio.create_task(main.demo_turn(uuid.uuid4(), uuid.uuid4(), main.PutTurn(message='synthetic'), 1)) for _ in range(100)]
    await eventually(lambda: len(store.calls) == 8 and sum(t.done() for t in calls) == 92)
    assert store.calls == ['owned'] * 8
    store.release.set()
    results = await asyncio.gather(*calls, return_exceptions=True)
    assert sum(isinstance(r, APIError) and r.status == 503 for r in results) == 92


class BlockingDatabase:
    def __init__(self):
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.active = self.peak = 0

    def existing_turn(self, **args):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            assert self.release.wait(5)
            return None
        finally:
            with self.lock:
                self.active -= 1

    def emit(self, **args): return True
    def replay(self, **args): return 'synthetic snapshot'
    def lease(self, **args): return True
    def close(self): self.release.set()


async def test_cancelled_database_workers_retain_slots_and_reserve_control_and_execution():
    adapter = LocalStore.__new__(LocalStore)
    adapter.store = db = BlockingDatabase()
    calls = [asyncio.create_task(adapter.call('existing_turn')) for _ in range(8)]
    try:
        await eventually(lambda: db.active == 1)
        for call in calls: call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        assert adapter._lanes['preflight']['pending'] == 8
        for _ in range(100):
            with pytest.raises(APIError) as rejected:
                await adapter.call('existing_turn')
            assert rejected.value.status == 503
        # Four ongoing generators can emit/finish despite a cancelled burst.
        assert await asyncio.gather(*(adapter.call('emit', kind='answer.delta') for _ in range(4))) == [True] * 4
        assert await asyncio.gather(*(adapter.call('emit', kind='turn.finished', payload={'status': 'COMPLETED'}) for _ in range(4))) == [True] * 4
        assert await adapter.call('replay') == 'synthetic snapshot'
        assert await adapter.call('emit', payload={'status': 'CANCELLED'}) is True
        assert await adapter.call('lease', recover=True) is True
        assert db.peak == 1
    finally:
        await adapter.close()
    await eventually(lambda: adapter._lanes['preflight']['pending'] == 0)


async def test_preflight_gate_frees_capacity_after_storage_exception():
    limits = ResourceLimits()
    store = BlockedReads()
    store.release.set()
    with pytest.raises(APIError):
        await limits.preflight(store, 'binding')
    assert limits.preflight_reads == 0

@pytest.mark.parametrize('base', ['/demo/api/conversations', '/demo/api/test/conversations'])
async def test_demo_api_first_session_read_is_bounded(monkeypatch, base):
    from app import demo_api
    store = BlockedReads()
    runtime = runtime_for(store)
    monkeypatch.setattr(main.app.state, 'store', store, raising=False)
    monkeypatch.setattr(main.app.state, 'runtime', runtime, raising=False)
    monkeypatch.setattr(demo_api, 'same_origin', lambda request: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://test',
                                headers={'Authorization': 'Bearer test-team-token'},
                                cookies={'chat_session': main.SIGNER.dumps('synthetic-session', salt='demo')}) as client:
        calls = [asyncio.create_task(client.put(f'{base}/{uuid.uuid4()}/turns/{uuid.uuid4()}', json={'message': 'synthetic'})) for _ in range(100)]
        await eventually(lambda: len(store.calls) == 8 and sum(t.done() for t in calls) == 92)
        assert store.calls == ['session'] * 8
        assert all(t.result().status_code == 503 for t in calls if t.done())
        store.release.set()
        await asyncio.gather(*calls)


@pytest.mark.parametrize('base', ['/demo/conversations', '/demo/api/conversations', '/demo/api/test/conversations'])
async def test_full_preflight_leaves_demo_cancel_and_events_reachable(monkeypatch, base):
    from app import demo_api
    class RecoveryStore:
        async def call(self, method, **args):
            if method == 'session': return 1
            if method == 'owned': return None
            if method == 'binding': return {'protocolFixture': '/test/' in base}
            if method == 'replay': return [], {'status': 'COMPLETED'}
            raise AssertionError(method)
    runtime = runtime_for(RecoveryStore())
    runtime.resource_limits.preflight_reads = runtime.resource_limits.preflight_limit
    monkeypatch.setattr(main.app.state, 'store', runtime.store, raising=False)
    monkeypatch.setattr(main.app.state, 'runtime', runtime, raising=False)
    monkeypatch.setattr(main, 'same_origin', lambda request: None)
    monkeypatch.setattr(main, 'require_demo_access', lambda request: None)
    monkeypatch.setattr(demo_api, 'same_origin', lambda request: None)
    forwarded = []
    async def proxy(path, **kwargs):
        forwarded.append(path)
        return {'synthetic': True}
    monkeypatch.setattr(main, 'proxied', proxy)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://test',
                                headers={'Authorization': 'Bearer test-team-token'},
                                cookies={'chat_session': main.SIGNER.dumps('synthetic-session', salt='demo')}) as client:
        path = f'{base}/{uuid.uuid4()}/turns/{uuid.uuid4()}'
        assert (await client.put(path + '/cancellation')).status_code == 200
        assert (await client.get(path + '/events')).status_code == 200
        if '/api/' in base:
            assert (await client.get(path)).status_code == 200
    assert len(forwarded) == 2
    assert runtime.resource_limits.preflight_reads == 8

async def test_close_does_not_claim_to_cancel_a_checked_out_database_call():
    class CheckedOutDatabase(BlockingDatabase):
        def close(self): pass  # Like a checked-out psycopg query, already alive.
    adapter = LocalStore.__new__(LocalStore)
    adapter.store = db = CheckedOutDatabase()
    call = asyncio.create_task(adapter.call('existing_turn'))
    try:
        await eventually(lambda: db.active == 1)
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        await asyncio.wait_for(adapter.close(), .5)
        assert adapter._lanes['preflight']['pending'] == 1 and db.active == 1
        with pytest.raises(APIError) as rejected:
            await adapter.call('replay')
        assert rejected.value.status == 503
    finally:
        db.release.set()
    await eventually(lambda: adapter._lanes['preflight']['pending'] == 0)
    threads = [thread for lane in adapter._lanes.values() for thread in lane['executor']._threads]
    await eventually(lambda: not any(thread.is_alive() for thread in threads))
