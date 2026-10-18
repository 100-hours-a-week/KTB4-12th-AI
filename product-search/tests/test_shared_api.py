import asyncio
import json

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.exceptions import RequestValidationError
from search.common_api import router, contract
from search.models import SearchRequest
from tools.sync_catalog import sync, fetcher
from test_sync import sync_case


@pytest.fixture
def shared(service):
    for i, p in enumerate(service.products):
        p.update(view_count=50-i, source_updated_at='2026-10-16T00:00:00Z')
    service.manifest.update(model='jinaai/jina-embeddings-v5-text-nano-retrieval', model_sha256='a'*64,
        encoder_fingerprint='b'*64, precision='q4f16', query_prefix='Query: ', document_prefix='Document: ')
    app = FastAPI(); app.include_router(router); app.state.search = service
    # Exercise the production redacted validation response, not FastAPI's
    # default error renderer, which attempts to echo invalid Unicode inputs.
    from app import invalid
    app.add_exception_handler(RequestValidationError, invalid)
    return service, TestClient(app)


def test_contract_catalog_and_exact_product_vector_order(shared):
    s, c = shared; meta = c.get('/v1/catalog/contract').json()
    assert meta['distance'] == 'cosine' and len(meta['encoderFingerprint']) == 64
    body = c.get('/v1/catalog', params={'snapshotId': s.snapshot_id, 'limit': 2}).json()
    assert body['products'][0]['viewCount'] == 50
    assert body['products'][0]['parentCategoryId'] == s.products[0]['parent_category_id']
    ids = [s.products[2]['id'], s.products[0]['id'], s.products[2]['id']]
    result = c.post('/v1/embeddings/products', json={'snapshotId': s.snapshot_id, 'ids': ids}).json()
    assert result['ids'] == ids
    np.testing.assert_allclose(result['vectors'], s.vectors[[2,0,2]])


@pytest.mark.parametrize('path,extra', [('/embeddings/products', {'ids': [1]}),
    ('/embeddings/query', {'texts':['합성 취향']}), ('/vector-search', {'vector':[1.0]+[0.0]*767})])
def test_generation_mismatch_fails_closed(shared, path, extra):
    _, c = shared
    assert c.post('/v1'+path, json={'snapshotId':'stale', **extra}).status_code == 409


def test_pure_vector_cosine_is_not_hybrid_ranking(shared):
    s,c = shared; vector=s.vectors[3]
    result=c.post('/v1/vector-search', json={'snapshotId':s.snapshot_id, 'vector':vector.tolist(),
        'filters':{'includeProductIds':[p['id'] for p in s.products[:4]]},'limit':4}).json()
    scores=s.vectors @ vector
    expected=s._top_candidates(scores, np.array([True,True,True,True,False]),4)
    assert [h['productId'] for h in result['hits']]==[s.products[i]['id'] for i in expected]
    np.testing.assert_allclose([h['score'] for h in result['hits']], scores[expected],rtol=1e-6)


@pytest.mark.parametrize('magnitude', [1e-30, 1e30, 1e-300, 1e300, 5e-324, np.finfo(np.float64).max])
@pytest.mark.parametrize('direction', [1.0, -1.0])
def test_cosine_is_invariant_for_finite_nonzero_query_scale(shared, magnitude, direction):
    service, client = shared
    def search(scale):
        result = client.post('/v1/vector-search', json={'snapshotId': service.snapshot_id,
            'vector': [0.0, scale] + [0.0]*766, 'limit': 5})
        assert result.status_code == 200
        return result.json()['hits']
    actual = search(magnitude * direction)
    assert actual == search(direction)
    assert all(np.isfinite(hit['score']) and 0 < abs(hit['score']) <= 1 for hit in actual)


@pytest.mark.parametrize('magnitude', [1e-300, 1e300, np.finfo(np.float64).max])
def test_dense_extreme_queries_have_finite_unit_direction(shared, magnitude):
    service, client = shared
    def search(scale):
        response = client.post('/v1/vector-search', json={'snapshotId': service.snapshot_id,
            'vector': [scale]*768, 'limit': 5})
        assert response.status_code == 200
        return response.json()['hits']
    assert search(magnitude) == search(1.0)


@pytest.mark.parametrize('value', [0.0, -0.0, float('nan'), float('inf'), -float('inf')])
def test_zero_or_nonfinite_vectors_are_rejected(shared, value):
    service, client = shared
    response = client.post('/v1/vector-search', content=json.dumps({
        'snapshotId': service.snapshot_id, 'vector': [value] + [0.0]*767}),
        headers={'Content-Type': 'application/json'})
    assert response.status_code == 422


def test_query_uses_raw_text_and_no_request_persistence(shared):
    s,c=shared; seen=[]
    async def get(text, source):
        seen.append((text,source));return s.vectors[0],False
    s.embedding.get=get
    out=c.post('/v1/embeddings/query',json={'snapshotId':s.snapshot_id,'texts':['Synthetic TAG Case']})
    assert out.status_code==200 and seen==[('Synthetic TAG Case','profile')]
    assert 'Synthetic TAG' not in out.text


@pytest.mark.parametrize('value', ['', 'a'*2001, '😀'*1001, '\ud800'])
def test_query_limits_match_encoder_utf16_contract(shared, value):
    s,c=shared
    response=c.post('/v1/embeddings/query',content=json.dumps({
        'snapshotId':s.snapshot_id,'texts':[value]}),headers={'Content-Type':'application/json'})
    assert response.status_code==422


def test_profile_fields_are_required_not_silently_defaulted(shared):
    s,c=shared;del s.products[0]['view_count']
    assert c.get('/v1/catalog',params={'snapshotId':s.snapshot_id}).status_code==503


def test_shared_api_auth_and_invalid_vectors(shared, monkeypatch):
    s,c=shared;monkeypatch.setenv('SEARCH_SERVICE_TOKEN','synthetic-only')
    assert c.get('/v1/catalog/contract').status_code==401
    assert c.get('/v1/catalog/contract',headers={'Authorization':'Bearer synthetic-only'}).status_code==200
    assert c.post('/v1/vector-search',headers={'Authorization':'Bearer synthetic-only'},json={
        'snapshotId':s.snapshot_id,'vector':[0.0]*768}).status_code==422


@pytest.mark.asyncio
async def test_include_pool_never_widens_hybrid_scope(service):
    ident=service.products[-1]['id']
    result=await service.search(SearchRequest(query='',filters={'includeProductIds':[ident]}))
    assert [p['productId'] for p in result['hits']]==[ident]


def test_separate_sync_preserves_popularity_and_old_generation(sync_case):
    root,export,categories,calls,run=sync_case
    export['data']['products'][0]['viewCount']=123
    run();old=(root/'data/CURRENT').read_text()
    from search.snapshots import active_data_dir
    old_data=active_data_dir(root/'data');before=(old_data/'catalog.json').read_bytes()
    export['data']['products'][0]['viewCount']=456
    result=run()
    current=json.loads((active_data_dir(root/'data')/'catalog.json').read_text())
    assert current['products'][0]['view_count']==456 and result['embedDocuments']==0
    assert (old_data/'catalog.json').read_bytes()==before and (root/'data/CURRENT').read_text()!=old


@pytest.mark.parametrize('vector', [
    [1e30] + [0.0] * 767,
    [1e-30] + [0.0] * 767,
    [0.0] * 768,
    [float('nan')] + [0.0] * 767,
    [float('inf')] + [0.0] * 767,
    [1.0],
    ['synthetic private output'] * 768,
    [True] + [False] * 767,
    None,
])
def test_bad_encoder_output_never_returns_success_or_leaks_payload(shared, vector):
    service, client = shared

    async def broken_encoder(text, source):
        return vector, False

    service.embedding.get = broken_encoder
    response = client.post('/v1/embeddings/query', json={
        'snapshotId': service.snapshot_id, 'texts': ['합성 비공개 태그']})
    assert response.status_code == 503
    assert response.json() == {'detail': 'INVALID_ENCODER_RESULT'}
    assert '합성 비공개' not in response.text and 'private output' not in response.text


def test_valid_encoder_preserves_existing_float32_normalization(shared):
    service, client = shared
    vector = service.vectors[0].copy()

    async def encoder(text, source):
        return vector, False

    service.embedding.get = encoder
    response = client.post('/v1/embeddings/query', json={
        'snapshotId': service.snapshot_id, 'texts': ['합성 태그']})
    assert response.status_code == 200
    expected = vector / np.linalg.norm(vector)
    np.testing.assert_array_equal(response.json()['vectors'][0], expected)
