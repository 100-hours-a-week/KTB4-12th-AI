"""Synthetic pressure and lifecycle regressions; no model, DB or network service."""
import asyncio
import copy
from types import SimpleNamespace
import uuid
import httpx
import pytest
from starlette.responses import StreamingResponse
from app import main
from app.errors import APIError
from app.protocol_fixture import ProtocolFixture
from app.resource_limits import ResourceLimits, cleanup_turn
from app.runtime import Runtime
from test_turn_recovery import FaultStore


@pytest.fixture
async def admission(monkeypatch):
    monkeypatch.setenv('CHAT_MAX_PENDING_ADMISSIONS', '2')
    store = FaultStore()
    store.start_release.clear()
    runtime = object.__new__(Runtime)
    runtime.init_state(store, None, 'synthetic-owner')
    runtime.model = 'synthetic-unused'
    runtime.generate = store.generate
    monkeypatch.setattr(main.app.state, 'store', store, raising=False)
    monkeypatch.setattr(main.app.state, 'runtime', runtime, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                  base_url='https://test',
                                  headers={'Authorization': 'Bearer ' + main.TOKEN}) as client:
        yield client, store, runtime
    store.start_release.set()
    store.run_release.set()
    await runtime.stop_jobs()
    await asyncio.sleep(0)


def path():
    return f'{main.BASE}/{uuid.uuid4()}/turns/{uuid.uuid4()}'


async def wait_for(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def test_pressure_rejects_before_new_job_or_write_and_duplicate_still_recovers(admission):
    client, store, runtime = admission
    a, b, rejected = path(), path(), path()
    first = asyncio.create_task(client.put(a, json={'message': 'synthetic first'}))
    await wait_for(lambda: store.starts == 1)
    second = asyncio.create_task(client.put(b, json={'message': 'synthetic second'}))
    await wait_for(lambda: store.starts == 2)
    duplicate = asyncio.create_task(client.put(a, json={'message': 'synthetic first'}))
    conflict = await client.put(a, json={'message': 'synthetic changed'})
    assert conflict.status_code == 409
    response = await client.put(rejected, json={'message': 'synthetic third'})
    assert response.status_code == 503 and response.headers['retry-after'] == '2'
    assert response.json()['error']['code'] == 'SERVICE_UNAVAILABLE'
    assert set(response.json()['error']) == {'code', 'traceId'}
    assert len(runtime.jobs) == len(runtime.admissions) == store.starts == 2
    assert rejected.rsplit('/', 1)[-1] not in store.turns
    assert runtime.healthy
    store.start_release.set()
    assert [r.status_code for r in await asyncio.gather(first, second, duplicate)] == [202, 202, 202]
    await wait_for(lambda: store.generation_calls == 2)
    assert store.starts == 2
    store.run_release.set()
    await asyncio.gather(*list(runtime.jobs.values()))
    await asyncio.sleep(0)
    retry = await client.put(a, json={'message': 'synthetic first'})
    assert retry.status_code == 200 and retry.json()['data']['status'] == 'COMPLETED'
    assert store.generation_calls == 2
    await asyncio.sleep(0)
    assert not runtime.signals and not runtime.admissions


async def test_pending_guard_does_not_become_a_second_generation_quota(admission):
    client, store, runtime = admission
    store.start_release.set()
    for _ in range(3):
        assert (await client.put(path(), json={'message': 'synthetic accepted'})).status_code == 202
    # Accepted futures no longer consume the pre-admission guard. The original
    # storage-level generation cap remains responsible for actual active turns.
    await wait_for(lambda: store.generation_calls == 3)
    assert all(entry['accepted'].done() for entry in runtime.admissions.values())


async def test_full_guard_keeps_terminal_snapshot_recoverable_without_regeneration(admission):
    client, store, runtime = admission
    old = path()
    cid, tid = old.split('/')[-3], old.split('/')[-1]
    store.turns[tid] = {'turnId': tid, 'cid': cid, 'message': 'synthetic completed',
                        'status': 'COMPLETED', 'blocks': [], 'lastEventId': 1}
    requests = [asyncio.create_task(client.put(path(), json={'message': 'synthetic pending'}))
                for _ in range(2)]
    await wait_for(lambda: store.starts == 2)
    # A stored retry is a read and bypasses the new-work admission guard.
    assert (await client.put(old, json={'message': 'synthetic completed'})).status_code == 200
    snapshot = await client.get(old)
    assert snapshot.status_code == 200 and snapshot.json()['data']['status'] == 'COMPLETED'
    store.start_release.set()
    await asyncio.gather(*requests)
    retry = await client.put(old, json={'message': 'synthetic completed'})
    assert retry.status_code == 200 and retry.json()['data'] == snapshot.json()['data']
    assert store.starts == 2


async def test_cancelled_admission_frees_protective_slot_without_generation(admission):
    client, store, runtime = admission
    a = path()
    request = asyncio.create_task(client.put(a, json={'message': 'synthetic cancel'}))
    await store.start_entered.wait()
    cancel = await client.put(a + '/cancellation')
    assert cancel.status_code == 200 and cancel.json()['data']['status'] == 'CANCELLED'
    assert (await request).status_code == 503
    await asyncio.gather(*list(runtime.jobs.values()))
    await asyncio.sleep(0)
    assert not runtime.admissions and not runtime.signals and store.generation_calls == 0
    store.start_release.set()
    assert (await client.put(path(), json={'message': 'synthetic next'})).status_code == 202


def test_fixture_shares_primary_pending_and_stream_limits(admission):
    _, _, runtime = admission
    fixture = ProtocolFixture(runtime)
    assert fixture.resource_limits is runtime.resource_limits
    assert fixture.admissions is runtime.admissions and fixture.signals is runtime.signals


class SSEStore:
    def __init__(self):
        self.calls = []
        self.terminal = False
        self.failure = None

    async def call(self, method, **args):
        self.calls.append((method, args))
        if method == 'binding':
            return {'protocolFixture': False}
        assert method == 'replay'
        if self.failure:
            raise self.failure
        state = {'turnId': args['tid'], 'message': 'synthetic SSE', 'blocks': [],
                 'status': 'COMPLETED' if self.terminal else 'PROCESSING', 'lastEventId': 1}
        events = [] if args['after'] else [{'id': 1,
                    'event': 'turn.finished' if self.terminal else 'progress',
                    'data': state if self.terminal else {'message': 'synthetic progress'}}]
        return copy.deepcopy(events), state


class Request:
    url = SimpleNamespace(path=main.BASE)
    query_params = {}
    disconnected = False

    async def is_disconnected(self):
        return self.disconnected


@pytest.fixture
def sse(monkeypatch):
    monkeypatch.setenv('CHAT_MAX_SSE_CONNECTIONS', '3')
    monkeypatch.setenv('CHAT_MAX_SSE_PER_TURN', '2')
    store = SSEStore()
    runtime = object.__new__(Runtime)
    runtime.init_state(store, None, 'synthetic-owner')
    runtime.model = 'synthetic-unused'
    monkeypatch.setattr(main.app.state, 'store', store, raising=False)
    monkeypatch.setattr(main.app.state, 'runtime', runtime, raising=False)
    return store, runtime


async def open_stream(tid, request=None):
    return await main.events(uuid.uuid4(), tid, request or Request(), None, 0)


async def close_started(response):
    await response.body_iterator.__anext__()
    await response.body_iterator.aclose()


async def test_per_turn_and_total_caps_reject_before_storage_and_release_on_close(sse):
    store, runtime = sse
    tid = uuid.uuid4()
    a, b = await open_stream(tid), await open_stream(tid)
    count = len(store.calls)
    with pytest.raises(APIError) as error:
        await open_stream(tid)
    assert error.value.status == 503 and error.value.headers['Retry-After'] == '2'
    assert len(store.calls) == count
    c = await open_stream(uuid.uuid4())
    count = len(store.calls)
    with pytest.raises(APIError):
        await open_stream(uuid.uuid4())
    assert len(store.calls) == count
    await close_started(a)
    d = await open_stream(tid)
    for response in (b, c, d):
        await close_started(response)
    assert runtime.resource_limits.total_streams == 0
    assert not runtime.resource_limits.streams and not runtime.signals


async def test_stream_disconnect_never_cancels_generation_and_keeps_active_signal(sse):
    _, runtime = sse
    tid = uuid.uuid4()
    owner = asyncio.create_task(asyncio.Event().wait())
    runtime.jobs[str(tid)] = owner
    try:
        response = await open_stream(tid)
        await close_started(response)
        assert not owner.done() and str(tid) in runtime.signals
        assert runtime.resource_limits.total_streams == 0
    finally:
        runtime.jobs.pop(str(tid))
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        cleanup_turn(runtime, str(tid))
    assert not runtime.signals


async def test_initial_storage_error_and_cancellation_release_slots(sse):
    store, runtime = sse
    store.failure = APIError(404, 'synthetic absent turn')
    with pytest.raises(APIError):
        await open_stream(uuid.uuid4())
    assert runtime.resource_limits.total_streams == 0 and not runtime.signals
    store.failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await open_stream(uuid.uuid4())
    assert runtime.resource_limits.total_streams == 0


async def test_midstream_storage_failure_releases_refs_without_fabricating_terminal(sse):
    store, runtime = sse
    response = await open_stream(uuid.uuid4())
    first = await response.body_iterator.__anext__()
    assert 'event: progress' in first
    store.failure = APIError(503, 'synthetic storage outage')
    with pytest.raises(APIError):
        await response.body_iterator.__anext__()
    assert runtime.resource_limits.total_streams == 0 and not runtime.signals
    assert all(method != 'emit' for method, _ in store.calls)


async def test_response_cancelled_before_body_start_releases_slot(sse, monkeypatch):
    _, runtime = sse
    response = await open_stream(uuid.uuid4())

    async def interrupted(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(StreamingResponse, '__call__', interrupted)
    with pytest.raises(asyncio.CancelledError):
        await response({}, None, None)
    assert response.lease.released and runtime.resource_limits.total_streams == 0
    response.lease.release()
    assert runtime.resource_limits.total_streams == 0


async def test_completed_replays_do_not_accumulate_turn_resources(sse):
    store, runtime = sse
    store.terminal = True
    for _ in range(100):
        response = await open_stream(uuid.uuid4())
        frames = [frame async for frame in response.body_iterator]
        assert len(frames) == 1 and 'event: turn.finished' in frames[0]
    assert not runtime.signals and not runtime.resource_limits.streams
    assert runtime.resource_limits.total_streams == 0 and not runtime.cache


async def test_overload_preserves_http_error_schema_and_auth_boundary(sse):
    store, runtime = sse
    tid = uuid.uuid4()
    a, b = await open_stream(tid), await open_stream(tid)
    url = f'{main.BASE}/{uuid.uuid4()}/turns/{tid}/events'
    count = len(store.calls)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://test') as client:
        assert (await client.get(url)).status_code == 401
        response = await client.get(url, headers={'Authorization': 'Bearer ' + main.TOKEN})
        assert response.status_code == 503 and response.headers['retry-after'] == '2'
        assert set(response.json()) == {'message', 'error'}
        assert set(response.json()['error']) == {'code', 'traceId'}
        assert response.json()['error']['code'] == 'SERVICE_UNAVAILABLE'
    assert len(store.calls) == count
    await close_started(a)
    await close_started(b)
    assert not runtime.signals


@pytest.mark.parametrize('stream', [False, True])
async def test_demo_proxy_preserves_existing_retry_after_and_error_body(monkeypatch, stream):
    body = {'message': 'synthetic overload', 'error': {'code': 'SERVICE_UNAVAILABLE',
                                                      'traceId': str(uuid.uuid4())}}
    async with httpx.AsyncClient(base_url='https://test', transport=httpx.MockTransport(
            lambda request: httpx.Response(503, json=body, headers={'Retry-After': '2'}))) as proxy:
        monkeypatch.setattr(main.app.state, 'proxy', proxy, raising=False)
        response = await main.proxied('/synthetic', {'message': 'synthetic'} if not stream else None,
                                      stream=stream)
    assert response.status_code == 503 and response.headers['retry-after'] == '2'
    import json
    assert json.loads(response.body) == body


@pytest.mark.parametrize('name', ['CHAT_MAX_PENDING_ADMISSIONS', 'CHAT_MAX_SSE_CONNECTIONS',
                                 'CHAT_MAX_SSE_PER_TURN', 'CHAT_RESOURCE_RETRY_AFTER_SECONDS'])
@pytest.mark.parametrize('value', ['0', '-1', 'invalid'])
def test_invalid_protective_configuration_fails_explicitly(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        ResourceLimits()
