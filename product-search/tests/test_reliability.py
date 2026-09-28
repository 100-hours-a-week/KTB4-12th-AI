import asyncio
import json
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

import app as server
from search.feedback import restore, save, FeedbackError
from search.models import FeedbackRequest, SearchRequest
from search.embedding import SharedEmbedding
from search.health import SearchHealth
from search_client import SearchClient


class Archive:
    def __init__(self):
        self.rows = {}
        self.fail = False

    def put(self, id, event):
        if self.fail:
            raise OSError('test storage unavailable')
        self.rows[id] = event

    def events(self):
        return self.rows.values()


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'DB', tmp_path / 'old.sqlite3')
    server.init_db()
    with server.db() as con:
        con.execute('INSERT INTO searches VALUES(?,?,?,?)', ('search-1', '2026-09-28T00:00:00Z', '{}',
                    json.dumps({'hits': [{'productId': 1}], 'snapshotId': 'original'})))
    return server.db


def test_feedback_failure_retry_and_restore(database, tmp_path, monkeypatch):
    archive = Archive()
    body = FeedbackRequest(searchId='search-1', productId=1, verdict='relevant', submissionId='a'*32)
    archive.fail = True
    with pytest.raises(FeedbackError) as error:
        save(database, body, archive)
    assert error.value.status == 503
    with database() as con:
        assert con.execute('SELECT count(*) FROM feedback').fetchone()[0] == 0
    archive.fail = False
    assert save(database, body, archive) == save(database, body, archive)
    assert len(archive.rows) == 1
    monkeypatch.setattr(server, 'DB', tmp_path / 'replacement.sqlite3')
    server.init_db()
    restore(database, archive)
    restore(database, archive)
    assert save(database, body, archive)['saved']
    with database() as con:
        assert con.execute('SELECT count(*) FROM feedback').fetchone()[0] == 1
        assert json.loads(con.execute('SELECT response FROM searches').fetchone()[0])['snapshotId'] == 'original'
    with pytest.raises(FeedbackError) as error:
        save(database, body.model_copy(update={'note': 'different'}), archive)
    assert error.value.status == 409


def test_archive_survives_local_commit_failure(database, monkeypatch):
    import search.feedback as module
    archive = Archive()
    body = FeedbackRequest(searchId='search-1', productId=1, verdict='relevant', submissionId='b'*32)
    original = module.restore_event
    monkeypatch.setattr(module, 'restore_event', lambda *args: (_ for _ in ()).throw(OSError('disk failure')))
    with pytest.raises(OSError):
        save(database, body, archive)
    assert len(archive.rows) == 1
    monkeypatch.setattr(module, 'restore_event', original)
    restore(database, archive)
    assert save(database, body, archive)['saved']


async def test_client_authenticates_every_operation():
    seen = []
    async def respond(request):
        seen.append((request.url.path, request.headers.get('authorization'), request.headers['x-search-source']))
        return httpx.Response(200, json={})
    async with SearchClient('https://test', token='test-only', source='chat', transport=httpx.MockTransport(respond)) as client:
        await client.ready()
        await client.metadata()
        await client.search({'query': '컵'})
        await client.get_products([1], snapshot_id='s')
    assert len(seen) == 4 and all(auth == 'Bearer test-only' and source == 'chat' for _, auth, source in seen)


class Encoder:
    def __init__(self):
        self.calls = 0
        self.fail = False
    def start(self): pass
    def close(self): pass
    def encode(self, query):
        self.calls += 1
        if self.fail:
            raise RuntimeError('encoder unavailable')
        return np.ones(768, dtype=np.float32) / np.sqrt(768)


async def test_readiness_bypasses_caches_detects_failure_and_recovers(service, monkeypatch):
    encoder = Encoder()
    embedding = SharedEmbedding(encoder)
    await embedding.start()
    monkeypatch.setattr(service, 'embedding', embedding)
    monitor = SearchHealth(service)
    for name, value in {'search': service, 'health': monitor, 'ready': True}.items():
        monkeypatch.setattr(server.app.state, name, value, raising=False)
    try:
        assert await monitor.check()
        assert await monitor.check()
        assert encoder.calls == 2
        encoder.fail = True
        assert not await monitor.check()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
            assert (await client.get('/readyz')).status_code == 503
            assert (await client.get('/healthz')).json() == {'status': 'alive'}
            encoder.fail = False
            assert (await client.get('/readyz?probe=true')).status_code == 200
        embedding.task.cancel()
        await asyncio.gather(embedding.task, return_exceptions=True)
        assert not monitor.ready
    finally:
        await embedding.close()

async def test_actual_encoder_exit_and_timeout_change_readiness(tmp_path, service, monkeypatch):
    import shutil
    from search.embedding import Encoder as ProcessEncoder
    if shutil.which('node') is None:
        pytest.skip('Node is required')
    program = tmp_path / 'service.mjs'
    healthy = "import readline from 'node:readline';console.log(JSON.stringify({ready:true}));for await (const l of readline.createInterface({input:process.stdin})){console.log(JSON.stringify({vector:Array(768).fill(1)}));}"
    program.write_text(healthy)
    encoder = ProcessEncoder(tmp_path)
    embedding = SharedEmbedding(encoder)
    await embedding.start()
    monkeypatch.setattr(service, 'embedding', embedding)
    health = SearchHealth(service)
    try:
        assert await health.check()
        encoder.process.kill()
        encoder.process.wait()
        assert not health.ready
        assert await health.check()
        encoder.close()
        program.write_text("console.log(JSON.stringify({ready:true}));process.stdin.resume();")
        original = encoder._read_response
        monkeypatch.setattr(encoder, '_read_response', lambda timeout, message: original(min(timeout, 0.5), message))
        assert not await health.check()
        assert encoder.process is None
        program.write_text(healthy)
        assert await health.check()
    finally:
        await embedding.close()


def test_author_restore_and_legacy_event(database, tmp_path, monkeypatch):
    archive = Archive()
    body = FeedbackRequest(searchId='search-1', productId=1, verdict='relevant',
                           submissionId='c'*32, reporterName='에멧')
    save(database, body, archive)
    with pytest.raises(FeedbackError) as error:
        save(database, body.model_copy(update={'reporter_name': 'another'}), archive)
    assert error.value.status == 409
    monkeypatch.setattr(server, 'DB', tmp_path / 'author.sqlite3')
    server.init_db(); restore(database, archive)
    assert save(database, body, archive)['saved']
    del archive.rows['c'*32]['feedback']['reporterName']
    monkeypatch.setattr(server, 'DB', tmp_path / 'legacy.sqlite3')
    server.init_db(); restore(database, archive)
    assert save(database, body.model_copy(update={'reporter_name': ''}), archive)['saved']


async def test_busy_encoder_marks_readiness_false_after_deadline(monkeypatch):
    import threading
    import time
    started, release = threading.Event(), threading.Event()
    encoder = Encoder()
    original = encoder.encode
    def blocked(query):
        started.set()
        release.wait(timeout=5)
        return original(query)
    monkeypatch.setattr(encoder, 'encode', blocked)
    embedding = SharedEmbedding(encoder)
    await embedding.start()
    request = asyncio.create_task(embedding.get('busy', use_cache=False))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert embedding.ready
        embedding.operation_started = time.monotonic() - 21
        assert not embedding.ready
        release.set()
        await request
        assert embedding.ready
    finally:
        release.set()
        await embedding.close()
