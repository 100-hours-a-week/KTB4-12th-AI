import asyncio

import httpx
import pytest

import app as server
from search_client import SearchAPIError, SearchClient


@pytest.fixture
def api(service, tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'DB', tmp_path / 'qa.sqlite3')
    server.init_db()
    for name, value in {
        'search': service, 'ready': True,
        'active_searches': 0, 'active_profile_searches': 0,
    }.items():
        monkeypatch.setattr(server.app.state, name, value, raising=False)
    return httpx.ASGITransport(app=server.app)


async def test_chat_profile_and_qa_share_results_but_only_qa_is_persisted(api, service, monkeypatch):
    sources = []
    original = service.search

    async def capture(*args, **kwargs):
        sources.append(kwargs['source'])
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, 'search', capture)
    payload = {'query': '텀블러', 'filters': {'maxPrice': 50000}}
    async with httpx.AsyncClient(transport=api, base_url='http://test') as http:
        qa = (await http.post('/api/search', json=payload)).json()
    results = []
    for source in ('chat', 'profile'):
        async with SearchClient('http://test', source=source, transport=api) as client:
            ready = await client.ready()
            result = await client.search(payload, snapshot_id=ready['snapshotId'])
            assert result['hits'] == qa['hits']
            assert 'searchId' not in result and 'request' not in result
            assert all(hit['price'] <= 50000 for hit in result['hits'])
            details = await client.get_products(
                [hit['productId'] for hit in result['hits']] + [99999],
                snapshot_id=result['snapshotId'],
            )
            assert details['missingIds'] == [99999]
            assert len(details['products']) == len(result['hits'])
            results.append(result)
    assert sources == ['qa', 'chat', 'profile']
    assert service.embedding.calls == 1
    with server.db() as con:
        assert con.execute('SELECT count(*) FROM searches').fetchone()[0] == 1


async def test_snapshot_conflict_validation_and_no_match_remain_distinct(api, service):
    async with SearchClient('http://test', transport=api) as client:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await client.search({'query': '컵'}, snapshot_id='old')
        assert error.value.response.status_code == 409
        assert isinstance(error.value, SearchAPIError) and error.value.code == 'SNAPSHOT_MISMATCH'
        assert service.embedding.calls == 0
        with pytest.raises(httpx.HTTPStatusError) as error:
            await client.get_products([1], snapshot_id='old')
        assert error.value.response.status_code == 409
        for payload in ({'limit': 0}, {'filters': {'minPrice': 20, 'maxPrice': 10}},
                        {'filters': {'categoryIds': [99999]}}, {'typo': True}):
            with pytest.raises(httpx.HTTPStatusError) as error:
                await client.search(payload)
            assert error.value.response.status_code == 422
        result = await client.search({'query': '컵', 'filters': {'maxPrice': 1}})
        assert result['status'] == 'NO_MATCH' and result['hits'] == []
    async with httpx.AsyncClient(transport=api, base_url='http://test') as http:
        response = await http.post('/v1/search', json={}, headers={'X-Search-Source': 'unknown'})
        assert response.status_code == 422
        assert response.headers['cache-control'] == 'no-store'


async def test_execution_failure_is_not_reported_as_empty_results(api, service, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError('test encoder failure')

    monkeypatch.setattr(service, 'search', fail)
    async with SearchClient('http://test', transport=api) as client:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await client.search({'query': '컵'})
        assert error.value.response.status_code == 503
        assert isinstance(error.value, SearchAPIError) and error.value.code == 'ENCODER_FAILURE'
    assert server.app.state.active_searches == 0
    assert server.app.state.active_profile_searches == 0


async def test_profile_limit_leaves_room_for_chat_and_releases_slots(api, service, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.search
    count = 0

    async def slow_profile(*args, **kwargs):
        nonlocal count
        if kwargs['source'] == 'profile':
            count += 1
            if count == 4:
                entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, 'search', slow_profile)
    async with httpx.AsyncClient(transport=api, base_url='http://test') as http:
        jobs = [asyncio.create_task(http.post('/v1/search', json={})) for _ in range(4)]
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            busy = await http.post('/v1/search', json={})
            assert busy.status_code == 503 and busy.headers['retry-after'] == '1'
            chat = await http.post('/v1/search', json={}, headers={'X-Search-Source': 'chat'})
            assert chat.status_code == 200
        finally:
            release.set()
            responses = await asyncio.gather(*jobs)
        assert all(response.status_code == 200 for response in responses)
    assert server.app.state.active_searches == 0
    assert server.app.state.active_profile_searches == 0


async def test_readiness_reports_unavailable_before_startup(api, monkeypatch):
    monkeypatch.setattr(server.app.state, 'ready', False)
    async with SearchClient('http://test', transport=api) as client:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await client.ready()
        assert error.value.response.status_code == 503
        assert isinstance(error.value, SearchAPIError) and error.value.code == 'SEARCH_NOT_READY'


@pytest.mark.parametrize('invalid_id', [True, False, 1.0, '1', 0, -1, 9223372036854775808])
async def test_product_ids_reject_coercion_and_out_of_range_values(api, invalid_id):
    async with httpx.AsyncClient(transport=api, base_url='http://test') as http:
        requests = [
            ('/v1/products', {'ids': [invalid_id]}),
            ('/api/products', {'ids': [invalid_id]}),
            ('/v1/search', {'filters': {'excludeProductIds': [invalid_id]}}),
            ('/api/search', {'filters': {'excludeProductIds': [invalid_id]}}),
            ('/api/feedback', {'searchId': 'absent', 'productId': invalid_id, 'verdict': 'relevant'}),
        ]
        for endpoint, payload in requests:
            response = await http.post(endpoint, json=payload)
            assert response.status_code == 422, (endpoint, response.text)
    with server.db() as con:
        assert con.execute('SELECT count(*) FROM searches').fetchone()[0] == 0
        assert con.execute('SELECT count(*) FROM feedback').fetchone()[0] == 0


@pytest.mark.parametrize('service', [[1, 2, 3, 4, 9223372036854775807]], indirect=True)
async def test_signed_int64_max_survives_search_lookup_exclusion_and_qa_storage(api):
    largest = 9223372036854775807
    async with httpx.AsyncClient(transport=api, base_url='http://test') as http:
        qa_response = await http.post('/api/search', json={})
        assert qa_response.status_code == 200
        qa = qa_response.json()
        hit = next(hit for hit in qa['hits'] if hit['sourceProductId'] == 'source:4')
        assert type(hit['productId']) is int and hit['productId'] == largest
        assert 'id' not in hit and 'backendProductId' not in hit

        detail_response = await http.post('/v1/products', json={
            'ids': [largest, largest - 1, 9007199254740993], 'snapshotId': qa['snapshotId'],
        })
        assert detail_response.status_code == 200
        details = detail_response.json()
        assert [p['productId'] for p in details['products']] == [largest]
        assert details['products'][0]['sourceProductId'] == 'source:4'
        assert details['missingIds'] == [largest - 1, 9007199254740993]

        excluded_response = await http.post('/v1/search', json={
            'filters': {'excludeProductIds': [largest]},
        })
        assert excluded_response.status_code == 200
        excluded = excluded_response.json()
        assert {hit['productId'] for hit in excluded['hits']} == {1, 2, 3, 4}

        feedback = await http.post('/api/feedback', json={
            'searchId': qa['searchId'], 'productId': largest, 'verdict': 'relevant',
        })
        assert feedback.status_code == 200 and feedback.json()['saved']
        missing = await http.post('/api/feedback', json={
            'searchId': qa['searchId'], 'productId': None, 'verdict': 'missing', 'note': '기대한 상품',
        })
        assert missing.status_code == 200 and missing.json()['saved']
        replay = await http.get(f"/api/searches/{qa['searchId']}")
        assert replay.status_code == 200 and replay.json()['hits'] == qa['hits']
