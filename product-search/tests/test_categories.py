import copy
import hashlib
import json
import warnings

import httpx
import numpy as np
import pytest

import app as server
from search import SearchService
from search.identity import BIGINT_MAX


def write_catalog(service, catalog, *, empty=False):
    directory = service.data_dir
    raw = json.dumps(catalog, ensure_ascii=False).encode()
    (directory / 'catalog.json').write_bytes(raw)
    manifest = copy.deepcopy(service.manifest)
    manifest['catalog_sha256'] = hashlib.sha256(raw).hexdigest()
    manifest['product_ids'] = [p['id'] for p in catalog['products']]
    if empty:
        (directory / 'vectors.f32').write_bytes(b'')
        manifest.update(product_ids=[], token_counts=[], vectors_sha256=hashlib.sha256(b'').hexdigest())
    manifest['vectors_sha256'] = hashlib.sha256((directory / 'vectors.f32').read_bytes()).hexdigest()
    (directory / 'manifest.json').write_text(json.dumps(manifest))


@pytest.fixture
def categorized(service):
    catalog = copy.deepcopy(service.catalog)
    catalog['taxonomy']['categories'].extend([
        {'category_id': 3, 'source_category_id': 'group-empty', 'parent_id': None, 'category': '빈 대분류', 'category_group': '빈 대분류'},
        {'category_id': 13, 'source_category_id': 'kitchen', 'parent_id': 1, 'category': 'kitchen', 'category_group': '생활'},
        {'category_id': 14, 'source_category_id': 'empty-leaf', 'parent_id': 3, 'category': '빈 소분류', 'category_group': '빈 대분류'},
    ])
    catalog['products'][4].update(category_id=13, source_category_id='kitchen', category='kitchen')
    write_catalog(service, catalog)
    result = SearchService(service.data_dir, service.embedding)
    yield result
    result.close()


@pytest.fixture
async def category_http(categorized, monkeypatch):
    for name, value in {'search': categorized, 'active_searches': 0, 'active_profile_searches': 0}.items():
        monkeypatch.setattr(server.app.state, name, value, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        yield client


@pytest.mark.parametrize('mode', ['hybrid', 'lexical', 'dense'])
async def test_group_filters_expand_all_leaves_and_exclusion_wins(categorized, mode):
    parent = await categorized.search({'query': '머그컵', 'mode': mode, 'filters': {'categoryIds': [1]}})
    leaves = await categorized.search({'query': '머그컵', 'mode': mode, 'filters': {'categoryIds': [11, 13]}})
    assert parent['hits'] == leaves['hits'] and parent['eligibleCount'] == 4
    selected = await categorized.search({'mode': mode, 'filters': {'categoryIds': [1], 'excludeCategoryIds': [11]}})
    assert [hit['productId'] for hit in selected['hits']] == [5]
    excluded = await categorized.search({'mode': mode, 'filters': {'categoryIds': [11, 13], 'excludeCategoryIds': [1]}})
    assert excluded['hits'] == [] and excluded['eligibleCount'] == 0
    redundant = await categorized.search({'mode': mode, 'filters': {'categoryIds': [1, 11, 13]}})
    assert len(redundant['hits']) == 4
    assert len({hit['productId'] for hit in redundant['hits']}) == 4


@pytest.mark.parametrize('query', ['', '선물'])
@pytest.mark.parametrize('field', ['preferredCategoryIds', 'downrankCategoryIds'])
async def test_group_preferences_equal_leaf_preferences_without_removing_candidates(categorized, query, field):
    base = await categorized.search({'query': query})
    parent = await categorized.search({'query': query, 'preferences': {field: [1]}})
    leaves = await categorized.search({'query': query, 'preferences': {field: [11, 13]}})
    redundant = await categorized.search({'query': query, 'preferences': {field: [1, 11, 13]}})
    assert parent['hits'] == leaves['hits'] == redundant['hits']
    assert {hit['productId'] for hit in parent['hits']} == {hit['productId'] for hit in base['hits']}
    assert parent['candidateCount'] == base['candidateCount']
    if field == 'downrankCategoryIds':
        assert parent['hits'][0]['parentCategoryId'] == 2
    else:
        assert parent['hits'][0]['parentCategoryId'] == 1


async def test_empty_taxonomy_groups_are_valid_zero_match_filters(categorized):
    result = await categorized.search({'query': '컵', 'filters': {'categoryIds': [3]}})
    assert result['status'] == 'NO_MATCH'
    assert result['hits'] == [] and result['eligibleCount'] == 0
    assert categorized.embedding.calls == 0


async def test_metadata_and_product_details_keep_numeric_and_source_category_ids(category_http):
    metadata = (await category_http.get('/v1/metadata')).json()
    assert metadata['groups'] == [
        {'id': 1, 'name': '생활', 'sourceCategoryId': 'group-home', 'count': 4},
        {'id': 2, 'name': '전자', 'sourceCategoryId': 'group-electronics', 'count': 1},
        {'id': 3, 'name': '빈 대분류', 'sourceCategoryId': 'group-empty', 'count': 0},
    ]
    leaves = {category['id']: category for category in metadata['categories']}
    assert set(leaves) == {11, 12, 13, 14}
    assert leaves[11] == {'id': 11, 'name': 'cup', 'group': '생활', 'parentId': 1, 'sourceCategoryId': 'cup', 'count': 3}
    assert leaves[13]['count'] == 1 and leaves[14]['count'] == 0
    response = await category_http.post('/v1/products', json={'ids': [5]})
    product = response.json()['products'][0]
    assert product['categoryId'] == 13
    assert product['sourceCategoryId'] == 'kitchen'
    assert product['parentCategoryId'] == 1


@pytest.mark.parametrize('section,field', [
    ('filters', 'categoryIds'), ('filters', 'excludeCategoryIds'),
    ('preferences', 'preferredCategoryIds'), ('preferences', 'downrankCategoryIds'),
])
async def test_category_http_rejects_invalid_or_unknown_ids(category_http, section, field):
    for value in [True, False, 11.0, '11', 0, -1, BIGINT_MAX + 1, 99999]:
        response = await category_http.post('/v1/search', json={section: {field: [value]}})
        assert response.status_code == 422, (section, field, value, response.text)
    response = await category_http.post('/v1/search', json={section: {field: [1]}})
    assert response.status_code == 200


async def test_signed_bigint_max_category_is_not_rounded(service):
    catalog = copy.deepcopy(service.catalog)
    for category in catalog['taxonomy']['categories']:
        if category['category_id'] == 11:
            category['category_id'] = BIGINT_MAX
    for product in catalog['products']:
        if product['category_id'] == 11:
            product['category_id'] = BIGINT_MAX
    write_catalog(service, catalog)
    search = SearchService(service.data_dir, service.embedding)
    try:
        result = await search.search({'filters': {'categoryIds': [BIGINT_MAX]}})
        assert len(result['hits']) == 4
        assert all(type(hit['categoryId']) is int and hit['categoryId'] == BIGINT_MAX for hit in result['hits'])
        parent = await search.search({'filters': {'categoryIds': [1]}})
        assert parent['hits'] == result['hits']
        excluded = await search.search({'filters': {'excludeCategoryIds': [BIGINT_MAX]}})
        assert [hit['categoryId'] for hit in excluded['hits']] == [12]
        metadata = search.get_metadata()
        assert next(c for c in metadata['categories'] if c['sourceCategoryId'] == 'cup')['id'] == BIGINT_MAX
    finally:
        search.close()


@pytest.mark.parametrize('with_taxonomy', [False, True])
async def test_empty_catalog_search_and_metadata_are_valid(service, with_taxonomy):
    catalog = copy.deepcopy(service.catalog)
    catalog['products'] = []
    if not with_taxonomy:
        catalog['taxonomy']['categories'] = []
    write_catalog(service, catalog, empty=True)
    with warnings.catch_warnings(record=True) as captured:
        search = SearchService(service.data_dir, service.embedding)
    assert not captured
    try:
        assert search.vectors.shape == (0, 768)
        assert np.isfinite(search.vectors).all()
        for mode in ['lexical', 'hybrid', 'dense']:
            for query in ['', '컵']:
                result = await search.search({'query': query, 'mode': mode})
                assert result['status'] == 'NO_MATCH' and result['hits'] == []
                assert result['eligibleCount'] == result['candidateCount'] == 0
                assert result['hasMore'] is False
        assert search.embedding.calls == 0
        metadata = search.get_metadata()
        assert metadata['productCount'] == 0
        assert all(c['count'] == 0 for c in metadata['categories'] + metadata['groups'])
    finally:
        search.close()


@pytest.mark.parametrize('case', ['unknown-parent', 'cycle', 'duplicate-source', 'product-parent', 'product-source'])
def test_invalid_taxonomy_identity_is_rejected_before_serving(service, case):
    catalog = copy.deepcopy(service.catalog)
    categories = catalog['taxonomy']['categories']
    if case == 'unknown-parent':
        categories[2]['parent_id'] = 99999
    elif case == 'cycle':
        categories[0]['parent_id'] = 11
    elif case == 'duplicate-source':
        categories[2]['source_category_id'] = categories[3]['source_category_id']
    elif case == 'product-parent':
        catalog['products'][0]['parent_category_id'] = 2
    else:
        catalog['products'][0]['source_category_id'] = 'audio'
    write_catalog(service, catalog)
    with pytest.raises(ValueError):
        SearchService(service.data_dir, service.embedding)


async def test_new_backend_products_and_categories_may_have_no_source_identity(service):
    catalog = copy.deepcopy(service.catalog)
    for category in catalog['taxonomy']['categories']:
        category['source_category_id'] = None
    for product in catalog['products']:
        product['source_category_id'] = None
        product['source_product_id'] = None
    write_catalog(service, catalog)
    search = SearchService(service.data_dir, service.embedding)
    try:
        result = await search.search({'query': '선물', 'filters': {'categoryIds': [1]}})
        assert len(result['hits']) == 4
        assert all(hit['sourceProductId'] is None and hit['sourceCategoryId'] is None for hit in result['hits'])
        assert all(hit['categoryId'] == 11 and hit['parentCategoryId'] == 1 for hit in result['hits'])
        repeated = await search.search({'query': '선물', 'filters': {'categoryIds': [1]}})
        assert repeated['hits'] == result['hits']
        details = search.get_products([1, 2])
        assert all(p['sourceProductId'] is None for p in details['products'])
        metadata = search.get_metadata()
        assert all(c['sourceCategoryId'] is None for c in metadata['categories'] + metadata['groups'])
    finally:
        search.close()


async def test_source_less_tied_products_use_backend_ids_for_stable_order(service):
    catalog = copy.deepcopy(service.catalog)
    template = catalog['products'][0]
    catalog['products'] = [dict(template, id=id, source_product_id=None) for id in [5, 2, 4, 1, 3]]
    vectors = np.zeros((5, 768), dtype=np.float32)
    vectors[:, 0] = 1
    (service.data_dir / 'vectors.f32').write_bytes(vectors.tobytes())
    write_catalog(service, catalog)
    search = SearchService(service.data_dir, service.embedding)
    try:
        for mode in ['lexical', 'dense', 'hybrid']:
            result = await search.search({'query': '텀블러', 'mode': mode})
            assert [hit['productId'] for hit in result['hits']] == [1, 2, 3, 4, 5]
            assert all(hit['sourceProductId'] is None for hit in result['hits'])
    finally:
        search.close()
