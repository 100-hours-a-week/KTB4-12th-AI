import copy
import hashlib
import json
import numpy as np
import pytest
from search import SearchService
from search.models import SnapshotMismatch
from search.service import ALGORITHM
from search.version import snapshot_id


def copy_index(service, directory, *, catalog=None, manifest=None, vectors=None):
    directory.mkdir()
    catalog=copy.deepcopy(service.catalog) if catalog is None else catalog
    manifest=dict(service.manifest) if manifest is None else manifest
    raw=json.dumps(catalog,ensure_ascii=False).encode()
    vector_bytes=(service.data_dir/'vectors.f32').read_bytes() if vectors is None else vectors.tobytes()
    manifest.update(catalog_sha256=hashlib.sha256(raw).hexdigest(),vectors_sha256=hashlib.sha256(vector_bytes).hexdigest(),product_ids=[p['id'] for p in catalog['products']])
    (directory/'catalog.json').write_bytes(raw)
    (directory/'manifest.json').write_text(json.dumps(manifest))
    (directory/'vectors.f32').write_bytes(vector_bytes)
    return SearchService(directory,service.embedding)


async def test_rebuilt_vectors_reject_previous_page_version(service,tmp_path):
    first=await service.search({'query':'컵','mode':'dense','limit':2})
    vectors=np.frombuffer((service.data_dir/'vectors.f32').read_bytes(),dtype=np.float32).reshape(5,768)[::-1].copy()
    new=copy_index(service,tmp_path/'new-vectors',vectors=vectors)
    try:
        assert first['snapshotId']!=new.snapshot_id
        with pytest.raises(SnapshotMismatch):
            await new.search({'query':'컵','mode':'dense','limit':2,'offset':2},snapshot=first['snapshotId'])
        with pytest.raises(SnapshotMismatch): new.get_products([1],first['snapshotId'])
        rerun=await new.search({'query':'컵','mode':'dense','limit':2})
        assert [p['productId'] for p in rerun['hits']]!=[p['productId'] for p in first['hits']]
    finally:new.close()


async def test_freshness_only_refresh_keeps_page_version(service,tmp_path):
    first=await service.search({'query':'','limit':2})
    catalog=copy.deepcopy(service.catalog)
    catalog['export_generated_at']='2026-09-25T03:00:00Z'
    for product in catalog['products']: product['source_updated_at']='2026-09-25T02:00:00Z'
    refreshed=copy_index(service,tmp_path/'fresh',catalog=catalog)
    try:
        assert refreshed.snapshot_id==first['snapshotId']
        page=await refreshed.search({'query':'','limit':2,'offset':first['nextOffset']},snapshot=first['snapshotId'])
        assert not {p['productId'] for p in first['hits']} & {p['productId'] for p in page['hits']}
        assert refreshed.get_metadata()['exportGeneratedAt']==catalog['export_generated_at']
    finally:refreshed.close()


@pytest.mark.parametrize('field,value',[('price',123),('availability','unavailable'),('description','다른 상품 설명'),('image','/changed.webp')])
def test_observable_product_changes_invalidate_version(service,field,value):
    catalog=copy.deepcopy(service.catalog);catalog['products'][0][field]=value
    assert snapshot_id(catalog,service.manifest,ALGORITHM)!=service.snapshot_id


@pytest.mark.parametrize('key,value',[('model','another-model'),('encoder_fingerprint','changed-encoder'),('token_counts',[11]*5)])
def test_encoder_or_detail_metadata_changes_invalidate_version(service,key,value):
    manifest=dict(service.manifest);manifest[key]=value
    assert snapshot_id(service.catalog,manifest,ALGORITHM)!=service.snapshot_id


def test_rank_version_changes_invalidate_version(service):
    assert snapshot_id(service.catalog,service.manifest,ALGORITHM+'/changed')!=service.snapshot_id


def test_installed_encoder_versions_legacy_bundle_and_matches_published_manifest(service,tmp_path):
    first=SearchService(service.data_dir,encoder_fingerprint='installed-encoder-a')
    changed=SearchService(service.data_dir,encoder_fingerprint='installed-encoder-b')
    published=copy_index(service,tmp_path/'fingerprinted',manifest=dict(service.manifest,encoder_fingerprint='installed-encoder-a'))
    try:
        assert first.snapshot_id!=changed.snapshot_id
        assert first.snapshot_id==published.snapshot_id
    finally:
        first.close();changed.close();published.close()


async def test_empty_page_is_not_no_candidates(service):
    first=await service.search({'query':'','limit':2})
    assert first['status']=='OK' and first['nextOffset']==2
    exhausted=await service.search({'query':'','limit':2,'offset':5})
    assert exhausted['status']=='OK' and exhausted['candidateCount']==5
    assert exhausted['hits']==[] and exhausted['nextOffset'] is None and exhausted['hasMore'] is False
    empty=await service.search({'query':'','filters':{'minPrice':999999}})
    assert empty['status']=='NO_MATCH' and empty['candidateCount']==0 and empty['nextOffset'] is None


async def test_browse_can_visit_every_product_past_old_offset_limit(service,tmp_path):
    catalog=copy.deepcopy(service.catalog)
    catalog['products']=[dict(service.products[i%5],id=i+1,source_product_id=f'source:{i}') for i in range(5101)]
    vectors=np.zeros((5101,768),dtype=np.float32);vectors[:,0]=1
    manifest=dict(service.manifest,token_counts=[10]*5101)
    large=copy_index(service,tmp_path/'large',catalog=catalog,manifest=manifest,vectors=vectors)
    try:
        ids=[];offset=0
        while offset is not None:
            result=await large.search({'query':'','offset':offset,'limit':100},snapshot=large.snapshot_id)
            ids.extend(p['productId'] for p in result['hits'])
            assert result['hasMore']==(result['nextOffset'] is not None)
            offset=result['nextOffset']
        assert len(ids)==len(set(ids))==5101
        assert set(ids)==set(range(1,5102))
    finally:large.close()


async def test_available_or_unknown_filters_before_limit_and_ranking(service):
    result=await service.search({'query':'선물','filters':{'availability':'available_or_unknown'},'limit':4})
    assert {p['productId'] for p in result['hits']}=={1,2,3,5}
    assert result['eligibleCount']==4 and result['candidateCount']==4 and len(result['hits'])==4
    assert {p['availability'] for p in result['hits']}=={'available','unknown'}
