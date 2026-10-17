import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

import catalog_readiness as readiness
from search.reloading import ReloadingSearch
from search.service import SearchService


@pytest.fixture
def generations(service, tmp_path, monkeypatch):
    root = tmp_path / 'canonical'
    def publish(name, *, broken=False):
        directory = root / 'data' / 'generations' / name
        directory.mkdir(parents=True, exist_ok=True)
        catalog = dict(service.catalog, ai_catalog_version=name)
        raw = json.dumps(catalog).encode()
        vectors = (service.data_dir / 'vectors.f32').read_bytes()
        manifest = dict(service.manifest, catalog_sha256=hashlib.sha256(raw).hexdigest(), encoder_fingerprint='fp')
        (directory / 'catalog.json').write_bytes(raw)
        (directory / 'manifest.json').write_text(json.dumps(manifest))
        (directory / 'vectors.f32').write_bytes(b'bad' if broken else vectors)
        (root / 'data' / 'CURRENT').write_text(name)
        return directory
    initial = SearchService(publish('v1'), service.embedding)
    wrapper = ReloadingSearch(root, initial, service.embedding, encoder_fingerprint='fp')
    monkeypatch.setattr('search.reloading.encoder_contract', lambda *args: 'fp')
    state = SimpleNamespace(active='v1')
    monkeypatch.setattr(readiness, 'active_version', lambda url: state.active)
    yield wrapper, root, publish, state
    for current in {wrapper.current, initial}:
        current.close()


async def test_repeated_failed_generation_stays_unready_then_recovers(generations):
    service, root, publish, state = generations
    publish('v2', broken=True); state.active = 'v2'
    async def poll():
        await readiness.synchronize(service, 'synthetic', root, lambda *args: {'catalogVersion': 'v2'})
    for _ in range(3):
        with pytest.raises(ValueError):
            await poll()
        assert not await readiness.matches_database(service, 'synthetic')
        assert service.current.catalog['ai_catalog_version'] == 'v1'
    # Legacy refresh may return without raising; it must never imply readiness.
    await service.refresh()
    assert service.load_error and not await readiness.matches_database(service, 'synthetic')
    publish('v2')
    await poll()
    assert await readiness.matches_database(service, 'synthetic')


async def test_activation_during_projection_rejects_the_intermediate_generation(generations):
    service, root, publish, state = generations
    def materialize(*args):
        publish('v2'); state.active = 'v3'
        return {'catalogVersion': 'v2'}
    with pytest.raises(RuntimeError, match='active database'):
        await readiness.synchronize(service, 'synthetic', root, materialize)
    assert not await readiness.matches_database(service, 'synthetic')
    publish('v3')
    await readiness.synchronize(service, 'synthetic', root, lambda *args: {'catalogVersion':'v3'})
    assert await readiness.matches_database(service, 'synthetic')


async def test_reactivation_of_the_loaded_good_generation_clears_failure(generations):
    service, root, publish, state = generations
    publish('v2', broken=True); state.active = 'v2'
    with pytest.raises(ValueError):
        await service.refresh()
    assert service.load_error
    publish('v1'); state.active = 'v1'
    await readiness.synchronize(service, 'synthetic', root, lambda *args: {'catalogVersion':'v1'})
    assert await readiness.matches_database(service, 'synthetic')


async def test_inflight_reader_keeps_its_snapshot_during_activation(generations, monkeypatch):
    wrapper, root, publish, state = generations
    old = wrapper.current
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args, **kwargs):
        entered.set(); await release.wait()
        return old.catalog['ai_catalog_version']
    monkeypatch.setattr(old, 'search', delayed)
    request = asyncio.create_task(wrapper.search({}))
    await entered.wait()
    publish('v2'); state.active = 'v2'
    assert not await readiness.matches_database(wrapper, 'synthetic')
    await readiness.synchronize(wrapper, 'synthetic', root, lambda *args: {'catalogVersion':'v2'})
    assert old in wrapper.retired and await readiness.matches_database(wrapper, 'synthetic')
    release.set()
    assert await request == 'v1'
    assert old not in wrapper.retired
