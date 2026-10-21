import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

from console.products import MAX_IMAGE, MAX_LIST, ProductDisplay


CONFIG = {"database": "synthetic", "productDisplay": {
    "backendUrl": "http://backend.test", "imageOrigins": ["http://images.test"]}}
PHOTO = b"\x89PNG\r\n\x1a\nsynthetic-photo"


def product(ident=42, **fields):
    return {"productId": ident, "productName": "Public mug", "brandName": "Synthetic",
            "price": 2000, "thumbnailUrl": "http://images.test/mug.png?signature=private-synthetic", **fields}


def response(rows, **page):
    return {"data": {"products": rows, "pagination": {"hasNext": False, "nextCursor": None, **page}}}


def make(monkeypatch, handler, config=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer synthetic-do-not-forward", "Cookie": "private=synthetic"})
    display = ProductDisplay(config or CONFIG, client)
    monkeypatch.setattr(display, "product_name", lambda ident: "Public mug synthetic long name")
    return display, client


def test_public_lookup_exact_id_private_url_and_cookie_boundary(monkeypatch):
    async def run():
        requests = []
        def upstream(request):
            requests.append(request)
            assert "authorization" not in request.headers and "cookie" not in request.headers
            if request.url.host == "backend.test":
                assert request.method == "GET" and request.url.path == "/products"
                assert request.url.params["query"] == "Public mug synthetic"
                return httpx.Response(200, json=response([product(99), product()]))
            return httpx.Response(200, content=PHOTO, headers={"Content-Type": "image/png"})
        display, client = make(monkeypatch, upstream)
        async with client:
            metadata = await display.metadata(42)
            assert metadata == {"productId": 42, "productName": "Public mug", "brandName": "Synthetic",
                                "unitPrice": 2000, "imageUrl": "/api/products/42/image"}
            assert "signature" not in json.dumps(metadata)
            assert await display.image(42) == (PHOTO, "image/png")
            assert len(requests) == 2
            assert requests[1].url.host == "images.test"
    asyncio.run(run())


def test_shorter_query_and_bounded_cursor(monkeypatch):
    async def run():
        calls = []
        def upstream(request):
            calls.append(dict(request.url.params))
            if len(calls) == 4:
                return httpx.Response(200, json=response([product()]))
            return httpx.Response(200, json=response([product(99)], hasNext=True, nextCursor="next"))
        display, client = make(monkeypatch, upstream)
        async with client:
            assert (await display.metadata(42))["productId"] == 42
        assert calls == [{"query": "Public mug synthetic"}, {"query": "Public mug synthetic", "cursor": "next"},
                         {"query": "Public mug"}, {"query": "Public mug", "cursor": "next"}]
    asyncio.run(run())


def test_fulltext_title_prefix_failure_uses_bounded_title_terms_and_exact_id(monkeypatch):
    async def run():
        calls = []
        def upstream(request):
            query = request.url.params['query']; calls.append(query)
            rows = [product(99), product()] if query == '데스크테리어' else []
            return httpx.Response(200, json=response(rows))
        display, client = make(monkeypatch, upstream)
        monkeypatch.setattr(display, 'product_name', lambda ident: '책상 위 포인트! 핑크키티 연필꽂이 데스크테리어')
        async with client:
            assert (await display.metadata(42))['productId'] == 42
        assert calls[:2] == ['책상 위 포인트!', '책상 위']
        assert len(calls) <= 4 and calls[-1] == '데스크테리어'
    asyncio.run(run())


@pytest.mark.parametrize("url", ["http://unknown.test/x.png", "http://images.test.evil/x.png",
    "http://images.test:81/x.png", "http://name:password@images.test/x.png", "file:///secret",
    "data:image/svg+xml,<svg/>", "http://images.test/x.png#fragment", "//images.test/x.png"])
def test_image_origin_allowlist(monkeypatch, url):
    async def run():
        calls = []
        def upstream(request):
            calls.append(request)
            return httpx.Response(200, json=response([product(thumbnailUrl=url)]))
        display, client = make(monkeypatch, upstream)
        async with client:
            assert (await display.metadata(42))["imageUrl"] is None
            with pytest.raises(HTTPException) as exc: await display.image(42)
            assert exc.value.status_code == 404
            assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["list_redirect", "image_redirect", "html_image", "svg_image",
    "wrong_magic", "large_image", "large_list", "invalid_json", "wrong_id", "negative_price"])
def test_bad_upstreams_fail_closed(monkeypatch, fault):
    async def run():
        calls = []
        def upstream(request):
            calls.append(request)
            if request.url.host == "backend.test":
                if fault == "list_redirect": return httpx.Response(302, headers={"Location": "http://evil.test/private"})
                if fault == "large_list": return httpx.Response(200, content=b"x"*(MAX_LIST+1), headers={"Content-Type":"application/json"})
                if fault == "invalid_json": return httpx.Response(200, content=b"[]", headers={"Content-Type":"application/json"})
                row = product(99) if fault == "wrong_id" else product(price=-1) if fault == "negative_price" else product()
                return httpx.Response(200, json=response([row]))
            if fault == "image_redirect": return httpx.Response(302, headers={"Location": "http://evil.test/private"})
            if fault == "large_image": return httpx.Response(200, content=b"x"*(MAX_IMAGE+1), headers={"Content-Type":"image/png"})
            if fault == "html_image": return httpx.Response(200, content=b"<html/>", headers={"Content-Type":"text/html"})
            if fault == "svg_image": return httpx.Response(200, content=b"<svg/>", headers={"Content-Type":"image/svg+xml"})
            return httpx.Response(200, content=b"<script/>", headers={"Content-Type":"image/png"})
        display, client = make(monkeypatch, upstream)
        async with client:
            with pytest.raises(HTTPException) as exc:
                await display.image(42)
            assert exc.value.status_code in {404, 503}
            assert "evil" not in str(exc.value.detail) and "signature" not in str(exc.value.detail)
            assert all(request.url.host in {"backend.test", "images.test"} for request in calls)
            assert len(calls) <= 4
    asyncio.run(run())


@pytest.mark.parametrize("ident", [True, 0, -1, 1.0, "42", 2**53])
def test_invalid_ids_never_fetch(monkeypatch, ident):
    async def run():
        display, client = make(monkeypatch, lambda request: pytest.fail("must not fetch"))
        async with client:
            with pytest.raises(HTTPException) as exc: await display.metadata(ident)
            assert exc.value.status_code == 400
    asyncio.run(run())


def test_catalog_lookup_is_read_only(monkeypatch):
    calls = []
    class DB:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params=None): calls.append((sql, params)); return self
        def fetchone(self): return ("Public mug",)
    monkeypatch.setattr("console.products.psycopg.connect", lambda *args, **kwargs: DB())
    display = ProductDisplay(CONFIG, None)
    assert display.product_name(42) == "Public mug"
    assert calls[0][0] == "SET TRANSACTION READ ONLY"
    assert calls[1][0] == "SET LOCAL statement_timeout='2000ms'"
    assert calls[2][0].startswith("SELECT p.product->>'name'")
    assert calls[2][1] == (42,)


def test_unconfigured_display_never_fetches(monkeypatch):
    async def run():
        display, client = make(monkeypatch, lambda request: pytest.fail("must not fetch"), config={"database":"synthetic"})
        async with client:
            with pytest.raises(HTTPException) as exc: await display.metadata(42)
            assert exc.value.status_code == 503
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["expired", "network", "invalid_image"])
def test_failed_image_evicts_only_its_url_so_user_retry_fetches_fresh_metadata(monkeypatch, failure):
    async def run():
        calls = []
        version = "old"
        def upstream(request):
            calls.append((request.url.host, request.url.path))
            if request.url.host == "backend.test":
                row = product()
                row["thumbnailUrl"] = f"http://images.test/{version}.png"
                return httpx.Response(200, json=response([row]))
            if request.url.path == "/old.png":
                if failure == "network":
                    raise httpx.ReadError("synthetic image unavailable")
                if failure == "invalid_image":
                    return httpx.Response(200, content=b"invalid image", headers={"Content-Type": "image/png"})
                return httpx.Response(403)
            return httpx.Response(200, content=PHOTO, headers={"Content-Type": "image/png"})
        display, client = make(monkeypatch, upstream)
        async with client:
            original = await display.metadata(42)
            unrelated = ({"productId": 99}, None, float("inf"))
            display.cache[99] = unrelated
            version = "fresh"
            with pytest.raises(HTTPException):
                await display.image(42)
            assert 42 not in display.cache and display.cache[99] is unrelated
            assert await display.metadata(42) == original
            assert await display.image(42) == (PHOTO, "image/png")
            assert calls == [("backend.test", "/products"), ("images.test", "/old.png"),
                             ("backend.test", "/products"), ("images.test", "/fresh.png")]
    asyncio.run(run())


def test_failed_image_does_not_evict_a_concurrently_refreshed_url(monkeypatch):
    async def run():
        current = ({"productId": 42}, "http://images.test/fresh.png", float("inf"))
        def upstream(request):
            if request.url.host == "backend.test":
                return httpx.Response(200, json=response([product()]))
            display.cache[42] = current
            return httpx.Response(403)
        display, client = make(monkeypatch, upstream)
        async with client:
            await display.metadata(42)
            with pytest.raises(HTTPException):
                await display.image(42)
            assert display.cache[42] is current
    asyncio.run(run())


def test_console_routes_require_admin_and_serve_same_origin_image(monkeypatch):
    from console.tests.auth_support import configured_app, login

    async def run():
        calls = []
        app = configured_app(CONFIG)
        monkeypatch.setattr(app.state.products, "product_name", lambda ident: "Public mug synthetic")
        def upstream(request):
            calls.append(request)
            if request.url.host == "backend.test":
                return httpx.Response(200, json=response([product()]))
            return httpx.Response(200, content=PHOTO, headers={"Content-Type": "image/png"})
        await app.state.backend.http.aclose()
        app.state.backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app.state.products.http = app.state.backend.http
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as client:
                for path in ("/api/products/42", "/api/products/42/image", "/static/products.js"):
                    assert (await client.get(path)).status_code == 401
                assert calls == []
                await login(client)
                metadata = await client.get("/api/products/42")
                assert metadata.status_code == 200
                assert metadata.json()["imageUrl"] == "/api/products/42/image"
                assert "signature" not in metadata.text
                photo = await client.get(metadata.json()["imageUrl"])
                assert photo.status_code == 200 and photo.content == PHOTO
                assert photo.headers["Content-Type"] == "image/png"
                assert "img-src 'self' data:" in photo.headers["Content-Security-Policy"]
                assert photo.headers["X-Content-Type-Options"] == "nosniff"
                assert (await client.get("/static/products.js")).status_code == 200
                assert len(calls) == 2
    asyncio.run(run())
