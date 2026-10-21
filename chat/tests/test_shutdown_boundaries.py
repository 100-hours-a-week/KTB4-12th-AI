"""DB-free shutdown regressions, including the real psycopg pool wait queue.

The pool is deliberately starved without ever creating a database connection.
Linux SIGTERM and a real unavailable database require a separate runtime test.
"""
import asyncio
from contextlib import contextmanager
import threading
import time

import pytest
from psycopg.errors import LockNotAvailable
from psycopg_pool import ConnectionPool, PoolTimeout

from app import local_store, main
from app.store import Store
from test_bootstrap import BootstrapStore


def unavailable_store(monkeypatch):
    # Real queue/timeout/close behavior, no network, DNS, credentials or DB server.
    monkeypatch.setattr(ConnectionPool, '_add_connection', lambda *a, **kw: None)
    pool = ConnectionPool('', min_size=0, max_size=1, open=True)
    store = Store.__new__(Store)
    store.pool = pool
    return store


def test_release_times_out_in_real_pool_before_shutdown_grace(monkeypatch):
    store = unavailable_store(monkeypatch)
    finished = threading.Event()
    errors = []

    def release():
        try:
            store.lease(owner='owner-A', next_owner='owner-A', release=True)
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=release)
    start = time.monotonic()
    worker.start()
    try:
        completed = finished.wait(4)
        elapsed = time.monotonic() - start
    finally:
        store.close()  # Also wakes the pre-fix 30-second waiter on assertion failure.
        worker.join(2)
    assert completed, 'Lease release consumed the pool default30s instead of its own bound'
    assert len(errors) == 1 and isinstance(errors[0], PoolTimeout)
    assert 1.5 <= elapsed < 4 and not worker.is_alive()


async def test_closing_pool_drains_cancelled_heartbeat_thread(monkeypatch):
    store = unavailable_store(monkeypatch)
    adapter = local_store.LocalStore.__new__(local_store.LocalStore)
    adapter.store = store
    thread_finished = threading.Event()
    original = store.lease

    def observe(**kwargs):
        try:
            return original(**kwargs)
        finally:
            if kwargs.get('inspect'):
                thread_finished.set()

    monkeypatch.setattr(store, 'lease', observe)
    heartbeat = asyncio.create_task(adapter.call('lease', owner='owner-A', inspect=True))
    try:
        deadline = time.monotonic() + 1
        while store.pool.get_stats().get('requests_waiting', 0) < 1:
            assert time.monotonic() < deadline
            await asyncio.sleep(.005)
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        assert not thread_finished.is_set(), 'to_thread cancellation must not be mistaken for DB cancellation'
        with pytest.raises(PoolTimeout):
            await asyncio.wait_for(adapter.call('lease', owner='owner-A', next_owner='owner-A', release=True), 4)
    finally:
        await adapter.close()
    deadline = time.monotonic() + 1
    while not thread_finished.is_set():
        assert time.monotonic() < deadline
        await asyncio.sleep(.005)
    assert store.pool.closed


class LeaseDatabase:
    def __init__(self, owner, locked=False):
        self.owner, self.locked = owner, locked
        self.statements = []

    def execute(self, sql, args=None):
        self.statements.append((sql, args))
        if 'pg_advisory_xact_lock' in sql and self.locked:
            assert ("SET LOCAL statement_timeout = '1s'", None) in self.statements
            assert ("SET LOCAL lock_timeout = '1s'", None) in self.statements
            raise LockNotAvailable('synthetic held lock')
        if sql.startswith('UPDATE runtime_lease SET owner=') and self.owner == args[1]:
            self.owner = args[0]
        return self

    def fetchone(self):
        return {'owner': self.owner, 'active': True}


class LeasePool:
    def __init__(self, db): self.db, self.timeout = db, None
    @contextmanager
    def connection(self, timeout=None):
        self.timeout = timeout
        yield self.db


@pytest.mark.parametrize('current_owner', ['owner-A', 'replacement-owner'])
def test_bounded_release_preserves_owner_cas_and_tombstone(current_owner):
    store = Store.__new__(Store)
    db = LeaseDatabase(current_owner)
    store.pool = LeasePool(db)
    assert store.lease(owner='owner-A', next_owner='owner-A', release=True) is True
    assert store.pool.timeout == 2
    assert db.owner.startswith('released:') if current_owner == 'owner-A' else db.owner == current_owner
    assert not any(sql.startswith('DELETE') for sql, _ in db.statements)


def test_release_sets_sql_deadlines_before_waiting_for_advisory_lock():
    store = Store.__new__(Store)
    db = LeaseDatabase('owner-A', locked=True)
    store.pool = LeasePool(db)
    with pytest.raises(LockNotAvailable):
        store.lease(owner='owner-A', next_owner='owner-A', release=True)
    assert db.owner == 'owner-A'


async def test_actual_lifespan_closes_store_when_remote_release_stalls(monkeypatch):
    class StalledRelease(BootstrapStore):
        closed = False
        release_args = None
        async def call(self, method, **args):
            if method == 'lease' and args.get('release'):
                self.release_args = args
                await asyncio.Future()
            return await super().call(method, **args)
        async def close(self): self.closed = True

    store = StalledRelease()
    monkeypatch.setenv('STORE_MODE', 'postgres')
    monkeypatch.setenv('DATABASE_URL', 'synthetic-no-database')
    monkeypatch.setenv('CHAT_RUNTIME_MODE', 'mock')
    monkeypatch.setattr(local_store, 'LocalStore', lambda url: store)
    monkeypatch.setattr(main, 'LEASE_RELEASE_TIMEOUT_SECONDS', .04, raising=False)
    async def exercise():
        async with main.lifespan(main.app):
            for _ in range(100):
                if main.app.state.runtime.healthy: break
                await asyncio.sleep(.005)
            assert main.app.state.runtime.healthy
            owner = main.app.state.runtime.owner
        assert store.release_args == {'owner': owner, 'next_owner': owner, 'release': True}
        assert store.closed
    await asyncio.wait_for(exercise(), 1)


async def test_readyz_returns_retryable_503_for_lost_database(monkeypatch):
    import httpx
    from psycopg import OperationalError

    class LostDatabase(BootstrapStore):
        lose_connection = False
        async def call(self, method, **args):
            if method == 'health' and self.lose_connection:
                raise OperationalError('PRIVATE_SYNTHETIC_DATABASE_ENDPOINT')
            return await super().call(method, **args)

    store = LostDatabase()
    monkeypatch.setenv('STORE_MODE', 'postgres')
    monkeypatch.setenv('DATABASE_URL', 'synthetic-no-database')
    monkeypatch.setenv('CHAT_RUNTIME_MODE', 'mock')
    monkeypatch.setattr(local_store, 'LocalStore', lambda url: store)
    async with main.lifespan(main.app):
        for _ in range(100):
            if main.app.state.runtime.healthy: break
            await asyncio.sleep(.005)
        assert main.app.state.runtime.healthy
        store.lose_connection = True
        async with httpx.AsyncClient(base_url='https://test', transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False)) as client:
            response = await client.get('/readyz', headers={'Authorization': 'Bearer ' + main.TOKEN})
        assert response.status_code == 503
        assert response.headers['Retry-After'] == '5'
        assert 'PRIVATE_SYNTHETIC_DATABASE_ENDPOINT' not in response.text
