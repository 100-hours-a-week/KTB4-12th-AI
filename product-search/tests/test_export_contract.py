"""Synthetic Backend export cases; these do not claim a live Backend export call."""
import copy
from datetime import datetime, timezone

import pytest

from search.export_catalog import project


@pytest.fixture
def inputs():
    export = {'message': 'exported', 'data': {
        'generatedAt': '2026-09-25T03:00:00Z',
        'products': [{'productId': 1, 'name': '스피커', 'brand': '브랜드', 'description': '방수 기능',
                      'categoryId': 11, 'categoryName': '음향', 'price': 30000,
                      'available': True, 'updatedAt': '2026-09-25T02:00:00Z'}],
    }}
    # Shape checked against BE def0adce: CategoryListResponse + ApiResponse.success.
    categories = {'message': '상품 카테고리 목록을 조회했습니다.', 'data': {'categories': [
        {'categoryId': 10, 'name': '디지털', 'children': [{'categoryId': 11, 'name': '음향'}]},
    ]}}
    old = {'products': [{'id': 1, 'source_product_id': 'KAKAO_GIFT:1',
                         'image': '/images/one.avif', 'image_large': '/images/large.avif',
                         'image_fallback': '/images/one.webp', 'product_type': 'Shipping',
                         'product_url': 'https://example.com/1'}]}
    return [export, categories, old, {'KAKAO_GIFT:1': 1}, {'CAT-PARENT': 10, 'CAT-CHILD': 11}]


def test_projection_uses_export_values_and_preserves_source_enrichment(inputs):
    before = copy.deepcopy(inputs)
    result = project(*inputs)
    p = result['products'][0]
    assert p['id'] == 1 and p['source_product_id'] == 'KAKAO_GIFT:1'
    assert p['category_id'] == 11 and p['source_category_id'] == 'CAT-CHILD'
    assert p['parent_category_id'] == 10 and p['category_group'] == '디지털'
    assert p['image'] == '/images/one.avif' and p['product_type'] == 'Shipping'
    assert p['price'] == 30000 and p['availability'] == 'available'
    assert p['description_origin'] == 'backend-export'
    assert result['document_contract'] == 'backend-export/1'
    assert inputs == before


@pytest.mark.parametrize('value', [True, 1.0, '1', 0, -1, 9007199254740992])
@pytest.mark.parametrize('field', ['productId', 'categoryId'])
def test_product_wire_ids_are_strict_positive_safe_integers(inputs, field, value):
    inputs[0]['data']['products'][0][field] = value
    with pytest.raises(ValueError):
        project(*inputs)


@pytest.mark.parametrize('value', [True, 11.0, '11', 0, -1, 9007199254740992])
def test_hierarchy_wire_ids_are_strict_positive_safe_integers(inputs, value):
    inputs[1]['data']['categories'][0]['children'][0]['categoryId'] = value
    with pytest.raises(ValueError):
        project(*inputs)


def test_maximum_wire_id_is_preserved(inputs):
    largest = 9007199254740991
    inputs[0]['data']['products'][0]['productId'] = largest
    p = project(*inputs)['products'][0]
    assert p['id'] == largest and p['source_product_id'] is None


@pytest.mark.parametrize(('field', 'value'), [
    ('name', 1), ('brand', None), ('description', 1), ('categoryName', 11),
    ('price', True), ('price', 3.0), ('price', '3'), ('price', -1),
    ('available', 1), ('available', 'true'),
])
def test_export_fields_do_not_coerce_invalid_types(inputs, field, value):
    inputs[0]['data']['products'][0][field] = value
    with pytest.raises(ValueError):
        project(*inputs)


@pytest.mark.parametrize('level', ['envelope', 'data', 'product', 'hierarchy'])
def test_unknown_contract_fields_are_rejected(inputs, level):
    target = {'envelope': inputs[0], 'data': inputs[0]['data'],
              'product': inputs[0]['data']['products'][0], 'hierarchy': inputs[1]['data']['categories'][0]}[level]
    target['typo'] = True
    with pytest.raises(ValueError):
        project(*inputs)


@pytest.mark.parametrize('field', ['generatedAt', 'updatedAt'])
@pytest.mark.parametrize('value', ['2026-09-25T02:00:00', '2026-09-25T02:00:00+09:00', 1790301600,
                                   datetime(2026, 9, 25, 2, tzinfo=timezone.utc)])
def test_timestamps_require_explicit_utc_strings(inputs, field, value):
    target = inputs[0]['data'] if field == 'generatedAt' else inputs[0]['data']['products'][0]
    target[field] = value
    with pytest.raises(ValueError):
        project(*inputs)


def test_utc_offset_format_and_equal_export_generation_are_valid(inputs):
    inputs[0]['data']['generatedAt'] = '2026-09-25T03:00:00+00:00'
    inputs[2]['export_generated_at'] = '2026-09-25T03:00:00Z'
    assert project(*inputs)['export_generated_at'] == '2026-09-25T03:00:00+00:00'


def test_product_update_cannot_postdate_the_export(inputs):
    inputs[0]['data']['products'][0]['updatedAt'] = '2026-09-25T04:00:00Z'
    with pytest.raises(ValueError, match='exceeds'):
        project(*inputs)


def test_older_export_cannot_replace_the_active_catalog(inputs):
    inputs[2]['export_generated_at'] = '2026-09-25T04:00:00Z'
    with pytest.raises(ValueError, match='older'):
        project(*inputs)


@pytest.mark.parametrize('ids', [[1, 1], [2, 1]])
def test_duplicate_or_unordered_products_are_rejected(inputs, ids):
    original = inputs[0]['data']['products'][0]
    inputs[0]['data']['products'] = [dict(original, productId=value) for value in ids]
    with pytest.raises(ValueError, match='unique and ascending'):
        project(*inputs)


@pytest.mark.parametrize('category_id', [10, 12])
def test_products_must_reference_an_existing_leaf(inputs, category_id):
    inputs[0]['data']['products'][0]['categoryId'] = category_id
    with pytest.raises(ValueError, match='category missing'):
        project(*inputs)


def test_category_names_must_match_the_hierarchy(inputs):
    inputs[0]['data']['products'][0]['categoryName'] = '다른 이름'
    with pytest.raises(ValueError, match='name differs'):
        project(*inputs)


@pytest.mark.parametrize('location', ['parent', 'child', 'cross_level'])
def test_duplicate_category_ids_are_rejected(inputs, location):
    groups = inputs[1]['data']['categories']
    if location == 'parent':
        groups.append(copy.deepcopy(groups[0]))
    elif location == 'child':
        groups[0]['children'].append(copy.deepcopy(groups[0]['children'][0]))
    else:
        groups[0]['children'][0]['categoryId'] = groups[0]['categoryId']
    with pytest.raises(ValueError, match='Duplicate category'):
        project(*inputs)


@pytest.mark.parametrize('mapping', [{}, {'KAKAO_GIFT:different': 1}])
def test_known_source_identity_cannot_be_missing_or_reassigned(inputs, mapping):
    inputs[3] = mapping
    with pytest.raises(ValueError, match='mapping conflicts'):
        project(*inputs)


def test_price_and_availability_changes_keep_embedding_document_identity(inputs):
    baseline = project(*inputs)['products'][0]
    inputs[0]['data']['products'][0].update(price=10000, available=False, updatedAt='2026-09-25T03:00:00Z')
    changed = project(*inputs)['products'][0]
    assert changed['price'] == 10000 and changed['availability'] == 'unavailable'
    assert changed['document'] == baseline['document']
    assert changed['document_hash'] == baseline['document_hash']
    assert changed['source_updated_at'] != baseline['source_updated_at']


def test_empty_export_and_empty_hierarchy_are_a_valid_empty_projection(inputs):
    inputs[0]['data']['products'] = []
    inputs[1]['data']['categories'] = []
    result = project(*inputs)
    assert result['products'] == [] and result['taxonomy']['categories'] == []
    assert result['export_generated_at']


def test_new_product_does_not_invent_source_identity_or_images(inputs):
    inputs[0]['data']['products'][0].update(productId=2, description=None, price=0)
    p = project(*inputs)['products'][0]
    assert p['source_product_id'] is None and p['description'] == ''
    assert p['image'] == p['image_large'] == p['image_fallback'] == ''
    assert p['product_type'] is None and p['price'] == 0
