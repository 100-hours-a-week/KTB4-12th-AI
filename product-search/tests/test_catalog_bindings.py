"""Synthetic immutable-generation retention; no encoder or network startup."""

import httpx
import pytest
from fastapi import FastAPI
from search.common_api import router
from search.reloading import ReloadingSearch


@pytest.fixture
async def bound_search(service, tmp_path, monkeypatch):
    for product in service.products:
        product.update(view_count=1, source_updated_at="2026-10-16T00:00:00Z")
    closed = []

    def generation(name):
        return SimpleGeneration(service, name, tmp_path / name, closed)

    old, new = generation("old"), generation("new")
    owner = ReloadingSearch(
        tmp_path, old, service.embedding, encoder_fingerprint="synthetic"
    )
    monkeypatch.setattr("search.reloading.active_data_dir", lambda _: new.data_dir)
    monkeypatch.setattr(owner, "_load_candidate", lambda _: new)
    monkeypatch.setenv("SEARCH_SERVICE_TOKEN", "synthetic")
    app = FastAPI()
    app.include_router(router)
    app.state.search = owner
    async with httpx.AsyncClient(
        base_url="http://synthetic",
        transport=httpx.ASGITransport(app=app),
        headers={"Authorization": "Bearer synthetic"},
    ) as client:
        yield owner, client, old, new, closed
    await owner.close()


class SimpleGeneration:
    def __init__(self, source, snapshot, data_dir, closed):
        self.__dict__.update(source.__dict__)
        self.snapshot_id = snapshot
        self.data_dir = data_dir
        self._closed = closed

    def close(self):
        self._closed.append(self.snapshot_id)


async def test_retained_generation_survives_reload_until_explicit_release(bound_search):
    owner, client, old, new, closed = bound_search
    response = await client.post("/v1/catalog/bindings", json={"snapshotId": "old"})
    assert response.status_code == 200 and response.json()["leaseSeconds"] == 600
    ident = response.json()["bindingId"]
    await owner.refresh()
    assert owner.current is new and old in owner.retired and not closed
    assert (await client.get("/v1/catalog/contract")).json()["snapshotId"] == "new"
    assert (
        await client.get("/v1/catalog", params={"snapshotId": "old"})
    ).status_code == 409
    retained = await client.get(
        "/v1/catalog", params={"snapshotId": "old", "bindingId": ident}
    )
    assert retained.status_code == 200 and retained.json()["snapshotId"] == "old"
    vectors = await client.post(
        "/v1/embeddings/products",
        json={"snapshotId": "old", "bindingId": ident, "ids": [2, 1]},
    )
    assert vectors.status_code == 200 and vectors.json()["ids"] == [2, 1]
    assert (await client.delete("/v1/catalog/bindings/" + ident)).status_code == 200
    assert closed == ["old"] and old not in owner.retired
    assert (await client.delete("/v1/catalog/bindings/" + ident)).status_code == 200
    assert closed == ["old"]


async def test_expired_binding_releases_old_generation_and_cannot_be_revived(
    bound_search,
):
    owner, client, old, _, closed = bound_search
    now = [1.0]
    owner.bindings.clock = lambda: now[0]
    ident = (
        await client.post("/v1/catalog/bindings", json={"snapshotId": "old"})
    ).json()["bindingId"]
    await owner.refresh()
    now[0] += 601
    response = await client.get("/v1/catalog/contract", params={"bindingId": ident})
    assert response.status_code == 409
    await owner.bindings.expire()
    assert closed == ["old"] and old not in owner.retired
    assert (
        await client.get("/v1/catalog/contract", params={"bindingId": ident})
    ).status_code == 409


async def test_bindings_have_capacity_snapshot_and_auth_guards(
    bound_search, monkeypatch
):
    owner, client, _, _, _ = bound_search
    owner.bindings.MAX_BINDINGS = 1
    assert (
        await client.post(
            "/v1/catalog/bindings",
            json={"snapshotId": "old"},
            headers={"Authorization": "Bearer wrong"},
        )
    ).status_code == 401
    assert not owner.bindings.entries
    assert (
        await client.post("/v1/catalog/bindings", json={"snapshotId": "stale"})
    ).status_code == 409
    ident = (
        await client.post("/v1/catalog/bindings", json={"snapshotId": "old"})
    ).json()["bindingId"]
    assert (
        await client.post("/v1/catalog/bindings", json={"snapshotId": "old"})
    ).status_code == 503
    assert (
        await client.get(
            "/v1/catalog", params={"snapshotId": "wrong", "bindingId": ident}
        )
    ).status_code == 409
    monkeypatch.delenv("SEARCH_SERVICE_TOKEN")
    assert (
        await client.post("/v1/catalog/bindings", json={"snapshotId": "old"})
    ).status_code == 503
    assert (
        await client.get("/v1/catalog/contract", params={"bindingId": ident})
    ).status_code == 503


async def test_binding_close_does_not_close_inflight_reader(bound_search):
    owner, client, old, _, closed = bound_search
    ident = (
        await client.post("/v1/catalog/bindings", json={"snapshotId": "old"})
    ).json()["bindingId"]
    await owner.refresh()
    # Same reader accounting used by pinned(): release the lease during a request.
    owner.readers[old] += 1
    await client.delete("/v1/catalog/bindings/" + ident)
    assert not closed
    await owner._release_reader(old)
    assert closed == ["old"]


async def test_db_publication_readiness_preserves_retained_jobs_and_cleanup(
    bound_search, monkeypatch
):
    import app as search_app

    owner, fixture_client, old, _, closed = bound_search
    ident = (
        await fixture_client.post("/v1/catalog/bindings", json={"snapshotId": "old"})
    ).json()["bindingId"]

    async def unavailable(*args):
        return False

    monkeypatch.setenv("SEARCH_CATALOG_DATABASE_URL", "synthetic-unused")
    monkeypatch.setattr(search_app, "database_ready", unavailable)
    monkeypatch.setattr(search_app.app.state, "search", owner, raising=False)
    async with httpx.AsyncClient(
        base_url="http://synthetic",
        transport=httpx.ASGITransport(app=search_app.app),
        headers={"Authorization": "Bearer synthetic"},
    ) as client:
        assert (await client.get("/v1/catalog/contract")).status_code == 503
        assert (
            await client.post("/v1/catalog/bindings", json={"snapshotId": "old"})
        ).status_code == 503
        assert (await client.post("/v1/search", json={"query": ""})).status_code == 503
        body = {"snapshotId": "old", "bindingId": ident, "ids": [1]}
        response = await client.post("/v1/embeddings/products", json=body)
        # Retained data is immutable, but deletion knowledge is global. A DB
        # outage must block product access; authenticated lease cleanup remains.
        assert response.status_code == 503
        assert (
            await client.post(
                "/v1/embeddings/products",
                json=body,
                headers={"Authorization": "Bearer wrong"},
            )
        ).status_code == 401
        await owner.refresh()
        assert old in owner.retired
        assert (await client.delete("/v1/catalog/bindings/" + ident)).status_code == 200
        assert closed == ["old"]
