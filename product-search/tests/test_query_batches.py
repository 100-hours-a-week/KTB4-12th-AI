"""Exercise the real bounded scheduler without models, credentials, or network."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fastapi import FastAPI

from search.common_api import QUERY_BATCH_SIZE, router
from search.embedding import SharedEmbedding


class SyntheticEncoder:
    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.fail_on = None
        self.active = 0
        self.peak_active = 0

    def start(self):
        pass

    @staticmethod
    def vector(text):
        vector = np.zeros(768, dtype=np.float32)
        vector[0] = 1
        vector[1] = sum(text.encode()) + 1
        return vector / np.linalg.norm(vector)

    def encode(self, text):
        self.calls.append(text)
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        self.entered.set()
        try:
            if not self.release.wait(3):
                raise RuntimeError('synthetic test watchdog')
            if text == self.fail_on:
                raise RuntimeError('synthetic private encoder failure')
            return self.vector(text)
        finally:
            self.active -= 1

    def close(self):
        self.release.set()


@pytest.fixture
async def batch_api():
    encoder = SyntheticEncoder()
    embedding = SharedEmbedding(encoder)
    await embedding.start()
    app = FastAPI()
    app.include_router(router)
    app.state.search = SimpleNamespace(snapshot_id='synthetic-batch', embedding=embedding)
    queue_sizes = []
    put = embedding.queue.put_nowait

    def record(item):
        put(item)
        queue_sizes.append(embedding.queue.qsize())

    embedding.queue.put_nowait = record
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            yield client, embedding, encoder, queue_sizes
    finally:
        encoder.release.set()
        await embedding.close()


def post(client, texts):
    return client.post('/v1/embeddings/query', json={
        'snapshotId': 'synthetic-batch', 'texts': texts,
    })


def assert_vectors(response, texts, encoder):
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['snapshotId'] == 'synthetic-batch'
    expected = np.array([encoder.vector(text) for text in texts])
    expected /= np.linalg.norm(expected, axis=1, keepdims=True)
    np.testing.assert_array_equal(result['vectors'], expected)


@pytest.mark.parametrize('count', [24, 25, QUERY_BATCH_SIZE])
async def test_cold_and_cached_batches_preserve_limit_order_and_vectors(batch_api, count):
    client, embedding, encoder, sizes = batch_api
    texts = [f'Synthetic TAG {i}' for i in range(count)]
    assert_vectors(await post(client, texts), texts, encoder)
    assert_vectors(await post(client, list(reversed(texts))), list(reversed(texts)), encoder)
    assert encoder.calls == texts
    assert encoder.peak_active == 1
    assert max(sizes) == 1
    assert embedding.queue.maxsize == 24
    assert not embedding.pending
    await asyncio.wait_for(embedding.queue.join(), 1)


async def test_33_texts_still_rejected_before_encoding(batch_api):
    client, _, encoder, _ = batch_api
    response = await post(client, [f'tag-{i}' for i in range(33)])
    assert response.status_code == 422
    assert not encoder.calls


async def test_concurrent_32_text_batches_keep_scheduler_bounded(batch_api):
    client, embedding, encoder, sizes = batch_api
    encoder.release.clear()
    batches = [[f'batch-{i}-tag-{j}' for j in range(32)] for i in range(8)]
    tasks = [asyncio.create_task(post(client, texts)) for texts in batches]
    try:
        assert await asyncio.to_thread(encoder.entered.wait, 1)
        assert embedding.queue.qsize() <= 7
        encoder.release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        for response, texts in zip(responses, batches):
            assert_vectors(response, texts, encoder)
        assert len(encoder.calls) == 256
        assert encoder.peak_active == 1
        assert max(sizes) <= 8
        assert not embedding.pending
    finally:
        encoder.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_duplicates_share_flight_across_concurrent_batches(batch_api):
    client, embedding, encoder, _ = batch_api
    encoder.release.clear()
    texts = ['Same RAW Tag', 'second tag', 'Same RAW Tag', 'third tag'] * 8
    tasks = [asyncio.create_task(post(client, texts)) for _ in range(4)]
    try:
        assert await asyncio.to_thread(encoder.entered.wait, 1)
        assert len(embedding.pending) == 1
        encoder.release.set()
        for response in await asyncio.wait_for(asyncio.gather(*tasks), 2):
            assert_vectors(response, texts, encoder)
        assert encoder.calls == ['Same RAW Tag', 'second tag', 'third tag']
        assert not embedding.pending
    finally:
        encoder.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_real_overload_is_503_with_retry_and_no_batch_tail(batch_api):
    client, embedding, encoder, _ = batch_api
    encoder.release.clear()
    tasks = [asyncio.create_task(embedding.get('active', 'chat'))]
    try:
        assert await asyncio.to_thread(encoder.entered.wait, 1)
        tasks.extend(asyncio.create_task(embedding.get(f'queued-{i}', 'qa')) for i in range(24))
        await asyncio.sleep(0)
        assert embedding.queue.full()
        texts = [f'rejected-{i}' for i in range(32)]
        response = await post(client, texts)
        assert response.status_code == 503
        assert response.json() == {'detail': 'ENCODER_BUSY'}
        assert response.headers['Retry-After'] == '1'
        assert not any(text in embedding.pending for text in texts)
        encoder.release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert not any(text in encoder.calls for text in texts)
        assert_vectors(await post(client, texts), texts, encoder)
    finally:
        encoder.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_encoder_failure_stops_batch_and_next_request_recovers(batch_api):
    client, embedding, encoder, _ = batch_api
    encoder.fail_on = 'broken'
    response = await post(client, ['first', 'broken', 'never submitted'])
    assert response.status_code == 503
    assert response.json() == {'detail': 'ENCODER_FAILURE'}
    assert 'private' not in response.text
    assert encoder.calls == ['first', 'broken']
    assert not embedding.pending
    encoder.fail_on = None
    assert_vectors(await post(client, ['broken', 'next']), ['broken', 'next'], encoder)


async def test_cancelled_batch_stops_tail_without_cancelling_shared_flight(batch_api):
    client, embedding, encoder, _ = batch_api
    encoder.release.clear()
    request = asyncio.create_task(post(client, ['shared', 'never submitted']))
    follower = None
    try:
        assert await asyncio.to_thread(encoder.entered.wait, 1)
        follower = asyncio.create_task(embedding.get('shared', 'chat'))
        await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert 'never submitted' not in embedding.pending
        encoder.release.set()
        vector, _ = await asyncio.wait_for(follower, 1)
        np.testing.assert_array_equal(vector, encoder.vector('shared'))
        assert encoder.calls == ['shared']
        assert_vectors(await post(client, ['after cancel']), ['after cancel'], encoder)
        assert not embedding.pending
    finally:
        encoder.release.set()
        await asyncio.gather(request, *([follower] if follower else []), return_exceptions=True)


async def test_batch_deadline_is_total_not_reset_for_each_text(batch_api, monkeypatch):
    import search.common_api as api

    client, embedding, encoder, _ = batch_api
    get, timeout = embedding.get, asyncio.timeout
    deadlines = []

    def controlled_timeout(seconds):
        assert seconds == api.QUERY_BATCH_TIMEOUT_SECONDS == 20
        deadline = timeout(None)
        deadlines.append(deadline)
        return deadline

    async def delayed(text, source):
        if text == 'second':
            # Expire the real timeout deterministically after the first text.
            # No wall-clock sleeps or assumptions about a busy CI machine.
            deadlines[0].reschedule(asyncio.get_running_loop().time())
            await asyncio.sleep(0)
        return await get(text, source)

    monkeypatch.setattr(api.asyncio, 'timeout', controlled_timeout)
    monkeypatch.setattr(embedding, 'get', delayed)
    response = await post(client, ['first', 'second', 'never submitted'])
    assert response.status_code == 503
    assert response.json() == {'detail': 'ENCODER_TIMEOUT'}
    assert response.headers['Retry-After'] == '1'
    assert len(deadlines) == 1
    assert encoder.calls == ['first']
    assert not embedding.pending


async def test_shutdown_during_batch_stops_tail_and_clears_pending(batch_api):
    client, embedding, encoder, _ = batch_api
    encoder.release.clear()
    request = asyncio.create_task(post(client, ['active', 'never submitted']))
    try:
        assert await asyncio.to_thread(encoder.entered.wait, 1)
        await embedding.close()
        response = await asyncio.wait_for(request, 1)
        assert response.status_code == 503
        assert response.json() == {'detail': 'ENCODER_FAILURE'}
        assert encoder.calls == ['active']
        assert not embedding.pending and embedding.queue.empty()
        await asyncio.wait_for(embedding.queue.join(), 1)
    finally:
        await asyncio.gather(request, return_exceptions=True)
