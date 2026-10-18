import copy
import hashlib
from uuid import UUID

import pytest

from catalog_db import encode, validate_generation


@pytest.fixture
def generation():
    version = UUID('00000000-0000-0000-0000-000000000001')
    catalog = {'products':[{'id':101,'source_product_id':'synthetic:101'}]}
    vectors = b'\0' * 8
    manifest = {'ai_catalog_version':str(version),'catalog_sha256':hashlib.sha256(encode(catalog)).hexdigest(),
                'vectors_sha256':hashlib.sha256(vectors).hexdigest(),'product_ids':[101],
                'source_product_ids':['synthetic:101'],'dimensions':2}
    return catalog, manifest, vectors, version


def test_valid_generation_returns_identical_bytes_without_mutating_inputs(generation):
    catalog, manifest, vectors, version = generation
    before = copy.deepcopy((catalog,manifest))
    assert validate_generation(catalog,manifest,vectors,version)==encode(catalog)
    assert (catalog,manifest)==before


@pytest.mark.parametrize('field,value',[
    ('ai_catalog_version','wrong'),('catalog_sha256','wrong'),('vectors_sha256','wrong'),
    ('product_ids',[102]),('source_product_ids',['synthetic:other']),('dimensions',3),
])
def test_mismatched_binding_preserves_the_original_failure(generation,field,value):
    catalog, manifest, vectors, version = generation
    manifest[field]=value
    with pytest.raises(ValueError,match='^Canonical product/vector/version mismatch$'):
        validate_generation(catalog,manifest,vectors,version)
