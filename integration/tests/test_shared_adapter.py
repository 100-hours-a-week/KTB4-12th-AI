"""Cross-service contract tests; run with both service dependency sets available."""
import asyncio
import importlib.util
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from search.common_api import router

# Reuse only the deterministic Search fixture; no application runtime is launched.
_path = Path(__file__).resolve().parents[2] / 'product-search/tests/conftest.py'
_spec = importlib.util.spec_from_file_location('synthetic_search_fixtures', _path)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
service = _fixture.service


@pytest.fixture
def shared(service):
    for i, p in enumerate(service.products):
        p.update(view_count=50-i, source_updated_at='2026-10-16T00:00:00Z')
    service.manifest.update(model='jinaai/jina-embeddings-v5-text-nano-retrieval', model_sha256='a'*64,
        encoder_fingerprint='b'*64, precision='q4f16', query_prefix='Query: ', document_prefix='Document: ')
    app = FastAPI(); app.include_router(router); app.state.search = service
    return service, TestClient(app)


def test_profiler_shared_adapter_matches_local_mmr_and_fails_on_rollover(shared):
    from profiling.search_api import SearchCatalog
    from profiling.pipeline import rank_products, DislikeMode
    from profiling.selection import select_pool, Tag
    from profiling.types import ProfileRequest, DislikedCategory, Review
    from profiling.ports import NoActiveCatalog
    s,c=shared
    remote=SearchCatalog('http://testserver','synthetic',client=c)
    rq=ProfileRequest(recipient_user_id=42,source_version=1,gift_preference='합성 취향',
        disliked_categories=[DislikedCategory(category_id=2,category_name='전자')],
        reviews=[Review(product_id=s.products[0]['id'],rating=5,review_text='합성 리뷰')])
    ranked=rank_products(rq,remote.products,dislike_mode=DislikeMode.PENALTY)
    class Local:
        def product_vectors(self,ids):return s.vectors[[s.by_id[i] for i in ids]]
        def encode(self,texts):return asyncio.run(s.embedding.get(texts[0],'profile'))[0][None,:].repeat(len(texts),axis=0)
    options={'allow_disliked':True,'tags':[Tag('특징','합성 취향')],'exclude_ids':frozenset([s.products[0]['id']])}
    assert select_pool(ranked,3,embedding=remote,**options)==select_pool(ranked,3,embedding=Local(),**options)
    old=remote.contract.copy();s.snapshot_id='changed'
    with pytest.raises(NoActiveCatalog):remote.active()
    with pytest.raises(NoActiveCatalog):remote.product_vectors([s.products[1]['id']])
    assert remote.contract==old


@pytest.mark.parametrize('fingerprint', [None, 'z'*64])
def test_shared_adapter_rejects_wrong_model_fingerprint(shared, fingerprint):
    from profiling.search_api import SearchCatalog
    s,c=shared;s.manifest['encoder_fingerprint']=fingerprint
    with pytest.raises(ValueError,match='Unversioned'):
        SearchCatalog('http://testserver','synthetic',client=c)


def test_shared_category_mapping_distinguishes_empty_from_unknown(shared):
    from profiling.search_api import SearchCatalog
    from profiling.types import DislikedCategory
    from profiling.ports import NoActiveCatalog
    source, client = shared
    source.catalog['taxonomy']['categories'].append({'category_id': 999, 'category': '빈 합성 분류',
                                        'parent_id': None, 'category_group': '빈 합성 분류'})
    remote = SearchCatalog('http://testserver', 'synthetic', client=client)
    remote.validate_disliked_categories([DislikedCategory(category_id=999, category_name='빈 합성 분류')])
    remote.validate_disliked_categories([DislikedCategory(category_id=9999, category_name='cup')])
    with pytest.raises(NoActiveCatalog, match='UNKNOWN_EXPLICIT_DISLIKE_CATEGORY'):
        remote.validate_disliked_categories([DislikedCategory(category_id=9999, category_name='없는 분류')])


def test_retained_profiler_refresh_and_same_snapshot_search_restart(shared, tmp_path, monkeypatch):
    """Real HTTP router + retained client + Emmet workers; no sockets or provider."""
    import copy
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from integration.profiler_rebind import ManagedSearch, load_retained_catalog
    from profiling.ports import NoActiveCatalog
    from profiling.search_api import SearchCatalog
    from profiling.supervisor import Supervisor
    from search.reloading import ReloadingSearch

    source, unused = shared
    unused.close()
    closed, clients, completed = [], [], []

    def generation(name, label):
        value = copy.copy(source)
        value.products = copy.deepcopy(source.products)
        value.catalog = copy.deepcopy(source.catalog)
        value.manifest = copy.deepcopy(source.manifest)
        value.snapshot_id = name
        value.data_dir = tmp_path / label
        value.close = lambda: closed.append(label)
        return value

    old, new = generation('old', 'old'), generation('new', 'new')
    owner = ReloadingSearch(tmp_path, old, source.embedding, encoder_fingerprint='b' * 64)
    monkeypatch.setattr('search.reloading.active_data_dir', lambda _: new.data_dir)
    monkeypatch.setattr(owner, '_load_candidate', lambda _: new)
    monkeypatch.setenv('SEARCH_SERVICE_TOKEN', 'synthetic')
    app = FastAPI()
    app.include_router(router)
    app.state.search = owner

    with TestClient(app, headers={'Authorization': 'Bearer synthetic'}) as client:
        class ClientView:
            closed = False

            def request(self, *args, **kwargs):
                assert not self.closed
                return client.request(*args, **kwargs)

            def close(self):
                self.closed = True

        def http_catalog(*args, **kwargs):
            view = ClientView()
            clients.append(view)
            return SearchCatalog(*args, client=view, **kwargs)

        monkeypatch.setattr('integration.profiler_rebind.SearchCatalog', http_catalog)
        settings = SimpleNamespace(SEARCH_BASE_URL='http://testserver', SEARCH_TOKEN=None, SERVICE_TOKEN='synthetic')
        factory = lambda: load_retained_catalog(settings)
        supervisor = Supervisor(profiling_slots=1, queue_max=3)
        supervisor.start()
        manager = ManagedSearch(factory(), supervisor, factory)
        entered, release = threading.Event(), threading.Event()

        def job(block=False):
            version, products = manager.active()
            if block:
                entered.set()
                assert release.wait(3)
            # Publication/refresh is attempted after the product list was read,
            # before the two subsequent vector calls of this same accepted job.
            assert manager._selected().snapshot == ('old' if str(version) == old_version else 'new')
            assert manager.product_vectors([products[0].productId]).shape == (1, 768)
            assert manager.encode(['합성 취향']).shape == (1, 768)
            completed.append(str(version))

        try:
            old_version = manager.check()
            assert manager.submit(job, True) and entered.wait(2)
            assert manager.submit(job)
            client.portal.call(owner.refresh)
            assert not closed and old in owner.retired
            assert client.get('/v1/catalog/contract').json()['snapshotId'] == 'new'
            with ThreadPoolExecutor(1) as pool:
                updating = pool.submit(manager.refresh, 'new', 3)
                with manager.condition:
                    assert manager.condition.wait_for(lambda: manager.paused, 2)
                assert not manager.submit(job)
                assert not updating.done() and manager.current.snapshot == 'old'
                release.set()
                assert updating.result(3)['changed']
            assert completed == [old_version, old_version]
            assert closed == ['old'] and clients[0].closed
            assert manager.check() != old_version and manager.current.snapshot == 'new'
            # Restart loses leases while publishing exactly the same data/contract.
            client.portal.call(owner.close)
            restarted = generation('new', 'restarted')
            owner = ReloadingSearch(tmp_path, restarted, source.embedding, encoder_fingerprint='b' * 64)
            app.state.search = owner
            with pytest.raises(NoActiveCatalog):
                manager.check()
            assert manager.refresh('new', 2)['changed']
            assert clients[1].closed and not manager.paused
            assert manager.current.snapshot == 'new'
            assert manager.submit(job)
            with manager.condition:
                assert manager.condition.wait_for(lambda: manager.pending == 0, 2)
            assert completed[-1] == manager.check()
        finally:
            release.set()
            manager.stop(3)
            manager.close()
            client.portal.call(owner.close)
        assert all(view.closed for view in clients)
        assert closed == ['old', 'new', 'restarted']
