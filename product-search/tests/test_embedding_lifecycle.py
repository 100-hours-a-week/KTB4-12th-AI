import asyncio
import hashlib
import json
import shutil
import threading
import time

import numpy as np
import pytest

from search.embedding import Encoder, SharedEmbedding


@pytest.fixture
def node_program(tmp_path):
    if shutil.which('node') is None:
        pytest.skip('Node is required for subprocess lifecycle tests')

    def write(program):
        (tmp_path / 'service.mjs').write_text(program)
        return tmp_path

    return write


@pytest.mark.parametrize('partial', [
    "process.stdout.write('{');",
    "process.stdout.write('{'); setInterval(()=>process.stdout.write(' '),15);",
])
def test_partial_or_trickling_response_obeys_total_deadline_and_recovers(node_program, monkeypatch, partial):
    path = node_program(
        "import readline from 'node:readline'; console.log(JSON.stringify({ready:true}));"
        "for await(const line of readline.createInterface({input:process.stdin})) {"
        + partial + "setTimeout(()=>process.exit(9),1500); await new Promise(()=>{});}")
    encoder = Encoder(path)
    encoder.start()
    child = encoder.process
    read = encoder._read_response
    monkeypatch.setattr(encoder, '_read_response', lambda timeout, message: read(min(timeout, .08), message))
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match='시간 초과'):
            encoder.encode('partial response')
        assert time.monotonic() - started < .8
        assert child.poll() is not None and child.stdin.closed and child.stdout.closed
        node_program("import readline from 'node:readline'; console.log(JSON.stringify({ready:true}));"
                     "for await(const line of readline.createInterface({input:process.stdin})) {"
                     "console.log(JSON.stringify({vector:Array(768).fill(1)}));}")
        monkeypatch.setattr(encoder, '_read_response', read)
        assert np.isclose(np.linalg.norm(encoder.encode('next request')), 1)
    finally:
        encoder.shutdown()


def test_oversized_unterminated_response_is_rejected(node_program):
    path = node_program("import readline from 'node:readline'; console.log(JSON.stringify({ready:true}));"
                        "for await(const line of readline.createInterface({input:process.stdin})) {"
                        "process.stdout.write(' '.repeat(2*1024*1024)); await new Promise(()=>{});}")
    encoder = Encoder(path)
    encoder.start()
    child = encoder.process
    try:
        with pytest.raises(RuntimeError, match='크기'):
            encoder.encode('oversized response')
        assert child.poll() is not None
    finally:
        encoder.shutdown()


async def test_startup_cancellation_reaps_child_and_executor(node_program, monkeypatch):
    import search.embedding as module

    path = node_program("process.stdout.write('{'); setInterval(()=>{},1000);")
    spawned = threading.Event()
    children = []
    popen = module.subprocess.Popen

    def capture(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        spawned.set()
        return child

    monkeypatch.setattr(module.subprocess, 'Popen', capture)
    shared = SharedEmbedding(Encoder(path))
    start = asyncio.create_task(shared.start())
    try:
        assert await asyncio.to_thread(spawned.wait, 2)
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start, 2)
        assert shared.task is None
        assert shared.encoder.process is None
        assert children[0].poll() is not None
        assert children[0].stdin.closed and children[0].stdout.closed
        with pytest.raises(RuntimeError, match='shutdown'):
            shared.pool.submit(lambda: None)
    finally:
        await shared.close()


async def test_cancel_during_verification_cannot_spawn_after_cleanup(node_program, monkeypatch):
    import search.embedding as module

    entered, release = threading.Event(), threading.Event()
    path = node_program("console.log(JSON.stringify({ready:true})); setInterval(()=>{},1000);")
    spawned = []
    popen = module.subprocess.Popen

    def capture(*args, **kwargs):
        spawned.append(True)
        return popen(*args, **kwargs)

    monkeypatch.setattr(module.subprocess, 'Popen', capture)

    def verify():
        entered.set()
        release.wait(2)

    shared = SharedEmbedding(Encoder(path, verify=verify))
    start = asyncio.create_task(shared.start())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        start.cancel()
        async def stopped():
            while not shared.encoder._stopping.is_set():
                await asyncio.sleep(.001)
        await asyncio.wait_for(stopped(), 1)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start, 2)
        assert shared.encoder.process is None
        assert shared.encoder._stopping.is_set()
        assert not spawned
    finally:
        release.set()
        await shared.close()


class BlockingEncoder:
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = []
        self.closed = False

    def start(self):
        pass

    def encode(self, query):
        self.calls.append(query)
        if query == 'blocker':
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError('test watchdog')
        return np.ones(768, dtype=np.float32)

    def shutdown(self):
        self.closed = True
        self.release.set()

    close = shutdown


async def test_chat_promotes_queued_shared_query_without_duplicate_work():
    encoder = BlockingEncoder()
    shared = SharedEmbedding(encoder)
    await shared.start()
    tasks = [asyncio.create_task(shared.get('blocker', 'chat'))]
    try:
        assert await asyncio.to_thread(encoder.entered.wait, 1)
        for query, source in [('shared', 'profile'), ('qa', 'qa'), ('shared', 'chat')]:
            tasks.append(asyncio.create_task(shared.get(query, source)))
            await asyncio.sleep(0)
        assert shared.queue.qsize() == 2
        encoder.release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert encoder.calls == ['blocker', 'shared', 'qa']
        assert not shared.pending
        await asyncio.wait_for(shared.queue.join(), .5)
    finally:
        encoder.release.set()
        await shared.close()


async def test_shutdown_rejects_active_and_orphaned_queue_without_draining_work():
    encoder = BlockingEncoder()
    shared = SharedEmbedding(encoder)
    await shared.start()
    active = asyncio.create_task(shared.get('blocker'))
    assert await asyncio.to_thread(encoder.entered.wait, 1)
    orphan = asyncio.create_task(shared.get('orphan', 'profile'))
    waiting = asyncio.create_task(shared.get('waiting', 'chat'))
    await asyncio.sleep(0)
    orphan.cancel()
    await asyncio.gather(orphan, return_exceptions=True)
    await asyncio.wait_for(shared.close(), 2)
    results = await asyncio.gather(active, waiting, return_exceptions=True)
    assert all(isinstance(result, RuntimeError) for result in results)
    assert encoder.calls == ['blocker'] and encoder.closed
    assert not shared.pending and shared.queue.empty()
    await asyncio.wait_for(shared.queue.join(), .5)
    with pytest.raises(RuntimeError):
        await shared.get('post-shutdown')
    await shared.close()  # repeated cleanup is safe


async def test_shutdown_interrupts_real_child_with_partial_response(node_program):
    path = node_program("import readline from 'node:readline'; import {writeFileSync} from 'node:fs';"
                        "console.log(JSON.stringify({ready:true}));"
                        "for await(const line of readline.createInterface({input:process.stdin})) {"
                        "writeFileSync('request-started',''); process.stdout.write('{'); await new Promise(()=>{});}")
    shared = SharedEmbedding(Encoder(path))
    await shared.start()
    child = shared.encoder.process
    request = asyncio.create_task(shared.get('stalled child'))
    try:
        async def requested():
            while not (path / 'request-started').exists():
                await asyncio.sleep(.005)
        await asyncio.wait_for(requested(), 2)
        await asyncio.wait_for(shared.close(), 2)
        with pytest.raises(RuntimeError, match='종료'):
            await request
        assert child.poll() is not None
        assert child.stdin.closed and child.stdout.closed
        await asyncio.wait_for(shared.queue.join(), .5)
    finally:
        await shared.close()
        await asyncio.gather(request, return_exceptions=True)


async def test_bootstrap_cancellation_runs_encoder_cleanup(tmp_path, monkeypatch):
    if shutil.which('node') is None:
        pytest.skip('Node is required for subprocess lifecycle tests')
    import search.bootstrap as bootstrap
    import search.embedding as module

    models = tmp_path / 'embedding/models'
    models.mkdir(parents=True)
    (models / 'model_q4f16.onnx_data').write_bytes(b'test weights')
    (tmp_path / 'embedding/service.mjs').write_text("process.stdout.write('{'); setInterval(()=>{},1000);")
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'manifest.json').write_text(json.dumps({'model_sha256': hashlib.sha256(b'test weights').hexdigest()}))
    monkeypatch.setattr(bootstrap, 'encoder_contract', lambda *_: 'test-fingerprint')
    children, instances = [], []
    spawned = threading.Event()
    popen, constructor = module.subprocess.Popen, bootstrap.SharedEmbedding

    def capture(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        spawned.set()
        return child

    def shared(encoder):
        instance = constructor(encoder)
        instances.append(instance)
        return instance

    monkeypatch.setattr(module.subprocess, 'Popen', capture)
    monkeypatch.setattr(bootstrap, 'SharedEmbedding', shared)

    async def startup():
        async with bootstrap.open_search(tmp_path):
            pytest.fail('partial ready message must not complete startup')

    task = asyncio.create_task(startup())
    try:
        assert await asyncio.to_thread(spawned.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert children[0].poll() is not None
        assert instances[0].encoder.process is None
        with pytest.raises(RuntimeError, match='shutdown'):
            instances[0].pool.submit(lambda: None)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for instance in instances:
            await instance.close()


async def test_cancelled_index_load_closes_its_eventual_result(tmp_path, monkeypatch):
    import search.bootstrap as bootstrap

    models = tmp_path / 'embedding/models'
    models.mkdir(parents=True)
    (models / 'model_q4f16.onnx_data').write_bytes(b'test weights')
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'manifest.json').write_text(json.dumps({'model_sha256': hashlib.sha256(b'test weights').hexdigest()}))
    monkeypatch.setattr(bootstrap, 'encoder_contract', lambda *_: 'test-fingerprint')
    entered, release = threading.Event(), threading.Event()
    closed = []

    class Embedding:
        def __init__(self, encoder):
            pass

        async def start(self):
            pass

        async def close(self):
            closed.append('embedding')

    class Index:
        def __init__(self, *args, encoder_fingerprint):
            assert encoder_fingerprint == 'test-fingerprint'
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test watchdog')

        def close(self):
            closed.append('index')

    monkeypatch.setattr(bootstrap, 'SharedEmbedding', Embedding)
    monkeypatch.setattr(bootstrap, 'SearchService', Index)

    async def startup():
        async with bootstrap.open_search(tmp_path):
            pytest.fail('cancelled startup must not publish the new index')

    task = asyncio.create_task(startup())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert closed == ['index', 'embedding']
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
