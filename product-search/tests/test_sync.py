import asyncio
import copy
import hashlib
import json
from pathlib import Path
import shutil
import numpy as np
import pytest
from search.export_catalog import encode
from search.reloading import ReloadingSearch
from search.service import SearchService
from search.snapshots import active_data_dir, sync_lock
from tools.sync_catalog import sync, fetcher


@pytest.fixture
def sync_case(tmp_path,service):
    root=tmp_path/'runtime-root'; data=root/'data'; data.mkdir(parents=True)
    for name in ['catalog.json','manifest.json','vectors.f32']:
        shutil.copy2(service.data_dir/name,data/name)
    (data/'product-id-map.jsonl').write_text(''.join(json.dumps({'sourceProductId':p['source_product_id'],'backendProductId':p['id']})+'\n' for p in service.products))
    (data/'category-id-map.jsonl').write_text(''.join(json.dumps({'sourceCategoryId':c['source_category_id'],'backendCategoryId':c['category_id']})+'\n' for c in service.catalog['taxonomy']['categories']))
    groups=[]
    for c in service.catalog['taxonomy']['categories']:
        if c['parent_id'] is None:
            groups.append({'categoryId':c['category_id'],'name':c['category'],'children':[{'categoryId':k['category_id'],'name':k['category']} for k in service.catalog['taxonomy']['categories'] if k['parent_id']==c['category_id']]})
    categories={'message':'ok','data':{'categories':groups}}
    export={'message':'ok','data':{'generatedAt':'2026-09-25T01:00:00Z','products':[{'productId':p['id'],'name':p['name'],'brand':p['brand'],'description':p['description'],'categoryId':p['category_id'],'categoryName':p['category'],'price':p['price'],'available':p['availability']=='available','updatedAt':'2026-09-25T00:00:00Z'} for p in service.products]}}
    calls=[]
    def embed(root,texts,directory):
        calls.append(list(texts))
        v=np.zeros((len(texts),768),dtype=np.float32); v[:,0]=1
        return v,[10]*len(texts)
    def run(apply=True,fetch=None,encoder=embed):
        return sync(root,fetch or (lambda:(copy.deepcopy(export),copy.deepcopy(categories))),embed=encoder,fingerprint=lambda *_:'test-encoder',apply=apply)
    return root,export,categories,calls,run


def test_preview_first_sync_reuse_and_removal(sync_case):
    root,export,categories,calls,run=sync_case
    preview=run(False)
    assert preview['embedDocuments']==5 and not (root/'data/CURRENT').exists() and not calls
    first=run(); assert first['embedDocuments']==5 and len(calls[-1])==5
    first_dir=active_data_dir(root/'data'); first_vectors=(first_dir/'vectors.f32').read_bytes()
    export['data']['generatedAt']='2026-09-25T02:00:00Z'
    export['data']['products'][0].update(price=999,available=True)
    second=run(); assert second['reuseVectors']==5 and second['embedDocuments']==0
    assert (active_data_dir(root/'data')/'vectors.f32').read_bytes()==first_vectors
    export['data']['products'][0]['name']='새 상품명'
    changed=run(); assert changed['embedDocuments']==1
    export['data']['products'].pop()
    removed=run(); assert removed['removed']==1 and removed['products']==4
    assert first_dir.exists()  # immutable previous generation retained


def test_failure_never_publishes_partial_index(sync_case):
    root,export,categories,calls,run=sync_case
    run(); pointer=(root/'data/CURRENT').read_bytes()
    def fail(): raise RuntimeError('HTTP failed')
    with pytest.raises(RuntimeError): run(fetch=fail)
    export['data']['products'][0]['name']='needs embedding'
    def invalid(*args): return np.zeros((1,767)),[10]
    with pytest.raises(ValueError): run(encoder=invalid)
    assert (root/'data/CURRENT').read_bytes()==pointer
    assert not list((root/'data/generations').glob('.staging-*'))


def test_writer_lock_precedes_fetch(sync_case):
    root,export,categories,calls,run=sync_case
    def forbidden(): pytest.fail('fetch must not run without lock')
    with sync_lock(root/'data'):
        with pytest.raises(RuntimeError,match='Another'): run(fetch=forbidden)


def test_empty_export_publishes_empty_index(sync_case):
    root,export,categories,calls,run=sync_case
    export['data']['products']=[]
    receipt=run()
    assert receipt['products']==0 and receipt['removed']==5
    service=SearchService(active_data_dir(root/'data'))
    try:
        assert service.get_metadata()['productCount']==0
        assert asyncio.run(service.search({'query':'텀블러'}))['status']=='NO_MATCH'
    finally: service.close()


def test_failed_build_and_stale_export_keep_pointer(sync_case,monkeypatch):
    root,export,categories,calls,run=sync_case
    run(); pointer=(root/'data/CURRENT').read_bytes()
    export['data']['generatedAt']='2026-09-24T01:00:00Z'
    with pytest.raises(ValueError): run()
    export['data']['generatedAt']='2026-09-25T02:00:00Z'
    def failure(*args): raise RuntimeError('lexical build failure')
    monkeypatch.setattr('tools.sync_catalog.SearchService',failure)
    with pytest.raises(RuntimeError): run()
    assert (root/'data/CURRENT').read_bytes()==pointer


@pytest.mark.asyncio
async def test_live_reload_preserves_inflight_search(sync_case,monkeypatch):
    root,export,categories,calls,run=sync_case
    initial=SearchService(root/'data')
    manager=ReloadingSearch(root,initial,None,encoder_fingerprint="test-encoder")
    old_snapshot=initial.snapshot_id
    started,release=asyncio.Event(),asyncio.Event()
    original=initial.search
    async def slow(*args,**kwargs):
        started.set(); await release.wait(); return await original(*args,**kwargs)
    initial.search=slow
    search=asyncio.create_task(manager.search({'query':'','mode':'lexical'}))
    await started.wait()
    run()
    monkeypatch.setattr('search.reloading.encoder_contract',lambda *_:'test-encoder')
    await manager.refresh()
    assert manager.snapshot_id!=old_snapshot and initial in manager.retired
    release.set()
    result=await search
    assert result['snapshotId']==old_snapshot and initial not in manager.retired
    assert (await manager.search({'query':'','mode':'lexical'}))['snapshotId']==manager.snapshot_id
    await manager.close()


@pytest.mark.asyncio
async def test_bad_published_generation_keeps_serving(sync_case,monkeypatch):
    root,export,categories,calls,run=sync_case
    initial=SearchService(root/'data'); manager=ReloadingSearch(root,initial,None,encoder_fingerprint="test-encoder")
    run(); monkeypatch.setattr('search.reloading.encoder_contract',lambda *_:'wrong')
    with pytest.raises(ValueError): await manager.refresh()
    assert manager.current is initial and manager.load_error=='catalog_load_failed'
    await manager.close()


@pytest.mark.asyncio
async def test_encoder_change_requires_runtime_restart(sync_case,monkeypatch):
    root,export,categories,calls,run=sync_case
    initial=SearchService(root/'data')
    manager=ReloadingSearch(root,initial,None,encoder_fingerprint='old-loaded-encoder')
    run()
    monkeypatch.setattr('search.reloading.encoder_contract',lambda *_:'test-encoder')
    with pytest.raises(ValueError): await manager.refresh()
    assert manager.current is initial
    await manager.close()


def test_noop_export_updates_freshness_without_invalidating_snapshot(sync_case):
    root,export,categories,calls,run=sync_case
    first=run()
    export['data']['generatedAt']='2026-09-25T02:00:00Z'
    export['data']['products'][0]['updatedAt']='2026-09-25T01:00:00Z'
    second=run()
    assert second['snapshotId']==first['snapshotId']
    assert second['reuseVectors']==5 and second['embedDocuments']==0
    service=SearchService(active_data_dir(root/'data'))
    try:
        assert service.snapshot_id==second['snapshotId']
        assert service.get_metadata()['exportGeneratedAt']=='2026-09-25T02:00:00+00:00'
    finally:service.close()


@pytest.mark.parametrize('empty', [False, True])
def test_fresh_export_needs_no_seed_or_mapping_and_can_update(sync_case, empty):
    root,export,categories,calls,run=sync_case
    shutil.rmtree(root/'data')
    (root/'embedding').mkdir()
    shutil.copy2(Path(__file__).resolve().parents[1]/'embedding/model.lock.json',root/'embedding/model.lock.json')
    if empty:
        export['data']['products']=[]
    preview=run(False)
    assert preview['removed']==0 and not calls and not (root/'data/CURRENT').exists()
    first=run()
    service=SearchService(active_data_dir(root/'data'))
    try:
        assert len(service.products)==(0 if empty else 5)
        assert all(p['source_product_id'] is None and p['image']=='' for p in service.products)
    finally:
        service.close()
    second=run()
    assert first['snapshotId']==second['snapshotId']
    assert second['embedDocuments']==0 and second['reuseVectors']==(0 if empty else 5)
    assert not (root/'data/product-id-map.jsonl').exists()


def test_failed_first_import_can_retry_without_partial_catalog(sync_case):
    root,export,categories,calls,run=sync_case
    shutil.rmtree(root/'data')
    (root/'embedding').mkdir()
    shutil.copy2(Path(__file__).resolve().parents[1]/'embedding/model.lock.json',root/'embedding/model.lock.json')
    def fail(*args): raise RuntimeError('encoder unavailable')
    with pytest.raises(RuntimeError): run(encoder=fail)
    assert not (root/'data/CURRENT').exists()
    assert not list((root/'data/generations').glob('.staging-*'))
    assert run()['products']==5


def test_partial_installation_is_not_overwritten(sync_case):
    root,export,categories,calls,run=sync_case
    (root/'data/manifest.json').unlink()
    before=(root/'data/catalog.json').read_bytes()
    with pytest.raises(FileNotFoundError): run()
    assert not calls and (root/'data/catalog.json').read_bytes()==before
    assert not (root/'data/CURRENT').exists()


def test_sample_initial_only_cannot_replace_catalog(sync_case):
    root,*_=sync_case
    def forbidden(): pytest.fail('must refuse before reading sample')
    with pytest.raises(ValueError,match='Refusing'):
        sync(root,forbidden,apply=True,initial_only=True)
