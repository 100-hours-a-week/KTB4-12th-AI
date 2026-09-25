import json

import httpx
import pytest
from pydantic import ValidationError

import app as server
from search.embedding import SearchBusy
from search.responses import (
    APIErrorResponse, FeedbackResponse, MetadataResponse, ProductsResponse,
    QASearchResponse, ReadyResponse, SearchHit, SearchResponse,
)
from search_client import SearchAPIError, SearchClient


@pytest.fixture
def contract_transport(service, tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'DB', tmp_path / 'contracts.sqlite3')
    server.init_db()
    for name, value in {'search': service, 'ready': True, 'active_searches': 0, 'active_profile_searches': 0}.items():
        monkeypatch.setattr(server.app.state, name, value, raising=False)
    return httpx.ASGITransport(app=server.app)


def schema_at(openapi, path, method, status):
    schema = openapi['paths'][path][method]['responses'][str(status)]['content']['application/json']['schema']
    assert '$ref' in schema, (path, method, status, schema)
    return openapi['components']['schemas'][schema['$ref'].rsplit('/', 1)[1]]


async def test_openapi_has_complete_success_and_actual_error_contracts(contract_transport):
    async with httpx.AsyncClient(transport=contract_transport, base_url='http://test') as http:
        schema = (await http.get('/openapi.json')).json()
    search = schema_at(schema, '/v1/search', 'post', 200)
    assert {'hits', 'nextOffset', 'snapshotId', 'status', 'candidateCount', 'timing'} <= set(search['required'])
    assert 'request' not in search['properties'] and 'searchId' not in search['properties']
    assert schema_at(schema, '/api/search', 'post', 200)['properties']['request']
    assert schema_at(schema, '/v1/products', 'post', 200)['properties']['missingIds']['items']['format'] == 'int64'
    assert schema_at(schema, '/v1/metadata', 'get', 200)['properties']['groups']
    assert schema_at(schema, '/readyz', 'get', 200)['properties']['exportGeneratedAt']
    hit_schema = schema['components']['schemas']['SearchHit']
    for field in ('productId', 'categoryId', 'parentCategoryId'):
        assert hit_schema['properties'][field]['type'] == 'integer'
        assert hit_schema['properties'][field]['format'] == 'int64'
        assert hit_schema['properties'][field]['minimum'] == 1
        assert hit_schema['properties'][field]['maximum'] == 9223372036854775807
    for field in ('sourceProductId', 'sourceCategoryId', 'productType'):
        assert {'type': 'null'} in hit_schema['properties'][field]['anyOf']
    for path, method, statuses in [
        ('/v1/search', 'post', [422, 409, 503]), ('/v1/products', 'post', [422, 409]),
        ('/api/feedback', 'post', [422, 404]), ('/readyz', 'get', [503]),
    ]:
        for status in statuses:
            assert set(schema_at(schema, path, method, status)['required']) == {'message', 'error'}
    assert 'HTTPValidationError' not in schema['components']['schemas']


@pytest.mark.parametrize('service', [[1, 2, 3, 4, 9223372036854775807]], indirect=True)
async def test_success_responses_validate_preserve_ids_and_freshness(contract_transport, service):
    service.catalog['export_generated_at'] = '2026-09-25T09:00:00+00:00'
    service.catalog['products'][0]['source_product_id'] = None
    service.catalog['products'][0]['source_category_id'] = None
    service.catalog['products'][0]['product_type'] = None
    async with httpx.AsyncClient(transport=contract_transport, base_url='http://test') as http:
        ready = ReadyResponse.model_validate((await http.get('/readyz')).json())
        metadata = MetadataResponse.model_validate((await http.get('/v1/metadata')).json())
        assert ready.exportGeneratedAt == metadata.exportGeneratedAt == '2026-09-25T09:00:00+00:00'
        response = await http.post('/v1/search', json={'limit': 2})
        assert response.status_code == 200
        result = SearchResponse.model_validate(response.json())
        assert result.nextOffset == 2 and result.hasMore
        assert result.hits[0].sourceProductId is None and result.hits[0].productType is None
        detail = await http.post('/v1/products', json={'ids': [9223372036854775807, 99999]})
        products = ProductsResponse.model_validate(detail.json())
        assert products.products[0].productId == 9223372036854775807
        assert products.missingIds == [99999]
        qa = QASearchResponse.model_validate((await http.post('/api/search', json={})).json())
        feedback = await http.post('/api/feedback', json={
            'searchId': qa.searchId, 'productId': 9223372036854775807, 'verdict': 'relevant',
        })
        assert FeedbackResponse.model_validate(feedback.json()).saved is True


async def test_old_qa_replay_does_not_invent_new_cursor_fields(contract_transport):
    async with httpx.AsyncClient(transport=contract_transport, base_url='http://test') as http:
        historic = (await http.post('/api/search', json={'limit': 2})).json()
        historic.pop('nextOffset')
        historic['snapshotId'] = 'historical-snapshot'
        with server.db() as con:
            con.execute('UPDATE searches SET response=? WHERE id=?', (json.dumps(historic), historic['searchId']))
        replay = await http.get(f"/api/searches/{historic['searchId']}")
        assert replay.status_code == 200
        assert replay.json() == historic


@pytest.mark.parametrize('payload,code', [
    ({'limit': 0}, 'INVALID_REQUEST'),
    ({'filters': {'categoryIds': [99999]}}, 'UNKNOWN_CATEGORY'),
    ({'filters': {'brands': ['nonexistent brand']}}, 'UNKNOWN_BRAND'),
    ({'snapshotId': 'stale'}, 'SNAPSHOT_MISMATCH'),
])
async def test_schema_and_semantic_errors_share_a_valid_documented_envelope(contract_transport, payload, code):
    async with SearchClient('http://test', transport=contract_transport) as client:
        with pytest.raises(SearchAPIError) as captured:
            await client.search(payload)
    error = captured.value
    assert isinstance(error, httpx.HTTPStatusError)
    assert error.code == code
    assert error.response.status_code == (409 if code == 'SNAPSHOT_MISMATCH' else 422)
    body = APIErrorResponse.model_validate(error.response.json())
    assert body.error.code == error.code
    assert 'detail' not in error.response.json() and 'issues' not in error.response.json()
    if code == 'INVALID_REQUEST':
        assert error.issues[0]['field'] == 'limit'
    else:
        assert error.issues == []


@pytest.mark.parametrize('failure,code', [
    (SearchBusy('queue full'), 'SEARCH_BUSY'), (TimeoutError('deadline'), 'SEARCH_TIMEOUT'),
    (RuntimeError('encoder failed'), 'ENCODER_FAILURE'),
])
async def test_execution_errors_keep_status_headers_and_machine_codes(contract_transport, service, monkeypatch, failure, code):
    async def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(service, 'search', fail)
    async with SearchClient('http://test', transport=contract_transport) as client:
        with pytest.raises(SearchAPIError) as captured:
            await client.search({'query': '선물'})
    error = captured.value
    assert error.response.status_code == 503 and error.code == code
    APIErrorResponse.model_validate(error.response.json())
    assert error.response.headers.get('retry-after') == (None if code == 'ENCODER_FAILURE' else '1')
    assert server.app.state.active_searches == server.app.state.active_profile_searches == 0


async def test_not_found_and_not_ready_are_distinct_documented_errors(contract_transport, monkeypatch):
    async with httpx.AsyncClient(transport=contract_transport, base_url='http://test') as http:
        response = await http.get('/api/searches/not-real')
        assert response.status_code == 404
        assert APIErrorResponse.model_validate(response.json()).error.code == 'NOT_FOUND'
    monkeypatch.setattr(server.app.state, 'ready', False)
    async with SearchClient('http://test', transport=contract_transport) as client:
        with pytest.raises(SearchAPIError) as captured:
            await client.ready()
    assert captured.value.code == 'SEARCH_NOT_READY'
    assert captured.value.response.status_code == 503


async def test_response_id_validation_does_not_coerce_bad_server_values(contract_transport):
    async with httpx.AsyncClient(transport=contract_transport, base_url='http://test') as http:
        original = (await http.post('/v1/search', json={})).json()['hits'][0]
    for field in ('productId', 'categoryId', 'parentCategoryId'):
        for value in (True, 1.0, '1', 0, 9223372036854775808):
            with pytest.raises(ValidationError):
                SearchHit.model_validate(dict(original, **{field: value}))


async def test_client_preserves_non_json_http_and_transport_failures():
    async with SearchClient('http://test', transport=httpx.MockTransport(lambda _: httpx.Response(502, text='proxy unavailable'))) as client:
        with pytest.raises(SearchAPIError) as captured:
            await client.metadata()
    assert captured.value.response.status_code == 502
    assert captured.value.code is None and captured.value.issues == []

    def offline(request):
        raise httpx.ConnectError('offline', request=request)
    async with SearchClient('http://test', transport=httpx.MockTransport(offline)) as client:
        with pytest.raises(httpx.ConnectError):
            await client.search({})
