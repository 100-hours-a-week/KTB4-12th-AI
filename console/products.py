"""Public product cards: exact catalog IDs, read-only BE lists, bounded photos."""
import asyncio
import json
import math
import re
import time
from urllib.parse import urlsplit

import httpx
import psycopg
from fastapi import HTTPException

MAX_IMAGE = 5 * 1024 * 1024
MAX_LIST = 256 * 1024
PHOTO_TYPES = {"image/avif", "image/webp", "image/png", "image/jpeg", "image/gif"}


def origin(url):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 33 for c in url):
        raise ValueError("invalid URL")
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or "\\" in url):
        raise ValueError("invalid URL")
    parsed.port  # Validate malformed ports without exposing the URL in errors.
    return parsed.scheme + "://" + parsed.netloc.lower()


def raster(body, media_type):
    if media_type == "image/png": return body.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg": return body.startswith(b"\xff\xd8\xff")
    if media_type == "image/gif": return body.startswith((b"GIF87a", b"GIF89a"))
    if media_type == "image/webp": return body[:4] == b"RIFF" and body[8:12] == b"WEBP"
    if media_type == "image/avif":
        return body[4:8] == b"ftyp" and any(body[i:i+4] in {b"avif", b"avis"} for i in range(8, min(64, len(body)), 4))
    return False


class ProductDisplay:
    def __init__(self, config, http):
        self.database = config.get("database")
        self.http = http
        self.cache = {}
        self.backend = None
        self.image_origins = set()
        options = config.get("productDisplay") or {}
        try:
            backend = options.get("backendUrl", "").rstrip("/")
            if origin(backend) != backend:
                raise ValueError("backend must be an origin")
            configured_origins = options.get("imageOrigins", [])
            if not isinstance(configured_origins, list) or len(configured_origins) > 10:
                raise ValueError("invalid image origins")
            for value in configured_origins:
                if origin(value) != value:
                    raise ValueError("image allowlist must contain origins")
            self.backend = backend
            self.image_origins = set(configured_origins)
        except (ValueError, TypeError, AttributeError):
            pass

    def product_name(self, ident):
        with psycopg.connect(self.database, connect_timeout=3) as db:
            db.execute("SET TRANSACTION READ ONLY")
            db.execute("SET LOCAL statement_timeout='2000ms'")
            row = db.execute(
                "SELECT p.product->>'name' FROM ai_catalog.generation_products p "
                "JOIN ai_catalog.catalog_state s ON p.generation_id=s.active_id "
                "WHERE s.singleton AND p.product_id=%s", (ident,)
            ).fetchone()
        if not row or not isinstance(row[0], str) or not row[0].strip():
            raise HTTPException(404, "PRODUCT_NOT_IN_CATALOG")
        return row[0]

    async def read(self, url, *, params=None, image=False):
        limit = MAX_IMAGE if image else MAX_LIST
        request = httpx.Request("GET", url, params=params,
            headers={"Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif" if image else "application/json"},
            extensions={"timeout": {part: 5 for part in ("connect", "read", "write", "pool")}})
        # A fresh Request deliberately excludes the client's cookies/default auth.
        response = await self.http.send(request, stream=True, follow_redirects=False, auth=None)
        try:
            if response.status_code != 200:
                raise HTTPException(503, "PRODUCT_UPSTREAM_UNAVAILABLE")
            kind = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if (image and kind not in PHOTO_TYPES) or (not image and kind != "application/json"):
                raise HTTPException(503, "PRODUCT_RESPONSE_INVALID")
            declared = response.headers.get("Content-Length")
            if declared is not None and (not declared.isdecimal() or int(declared) > limit):
                raise HTTPException(503, "PRODUCT_RESPONSE_TOO_LARGE")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > limit:
                    raise HTTPException(503, "PRODUCT_RESPONSE_TOO_LARGE")
                body.extend(chunk)
            return bytes(body), kind
        finally:
            await response.aclose()

    def photo_url(self, value):
        try:
            if origin(value) in self.image_origins:
                return value
        except (ValueError, TypeError):
            pass
        return None

    async def lookup(self, ident):
        if type(ident) is not int or not 1 <= ident <= 2**53 - 1:
            raise HTTPException(400, "PRODUCT_ID_INVALID")
        if not self.backend or not self.database:
            raise HTTPException(503, "PRODUCT_DISPLAY_NOT_CONFIGURED")
        saved = self.cache.get(ident)
        if saved and saved[2] > time.monotonic():
            return saved
        name = await asyncio.to_thread(self.product_name, ident)
        tokens = name.split()
        queries = list(dict.fromkeys(" ".join(tokens[:length]) for length in (3, 2)))
        # BE full-text phrase search can reject common short words in a title
        # prefix. Try a few longer title terms, still requiring the exact ID.
        terms = sorted(dict.fromkeys(re.findall(r"\w{3,}", name)), key=len, reverse=True)
        queries = list(dict.fromkeys([*queries, *terms[:2]]))
        for query in queries:
            cursor = None
            for _ in range(2):
                params = {"query": query}
                if cursor: params["cursor"] = cursor
                raw, _ = await self.read(self.backend + "/products", params=params)
                data = json.loads(raw).get("data", {})
                products = data.get("products")
                if not isinstance(products, list) or len(products) > 100:
                    raise HTTPException(503, "PRODUCT_RESPONSE_INVALID")
                for item in products:
                    if not isinstance(item, dict) or item.get("productId") != ident:
                        continue
                    price = item.get("price")
                    if (type(item.get("productId")) is not int
                            or not isinstance(item.get("productName"), str) or not item["productName"].strip()
                            or not isinstance(item.get("brandName"), str)
                            or type(price) not in {int, float} or not math.isfinite(price) or price < 0):
                        raise HTTPException(503, "PRODUCT_RESPONSE_INVALID")
                    url = self.photo_url(item.get("thumbnailUrl"))
                    result = {"productId": ident, "productName": item["productName"][:500],
                              "brandName": item["brandName"][:200], "unitPrice": price,
                              "imageUrl": f"/api/products/{ident}/image" if url else None}
                    saved = (result, url, time.monotonic() + 300)
                    if len(self.cache) >= 128 and ident not in self.cache:
                        self.cache.pop(next(iter(self.cache)))
                    self.cache[ident] = saved
                    return saved
                pagination = data.get("pagination", {})
                candidate = pagination.get("nextCursor")
                if (pagination.get("hasNext") is not True or not isinstance(candidate, str)
                        or not candidate or len(candidate) > 4096 or candidate == cursor):
                    break
                cursor = candidate
        raise HTTPException(404, "PRODUCT_NOT_FOUND")

    async def metadata(self, ident):
        try:
            async with asyncio.timeout(5):
                return (await self.lookup(ident))[0]
        except (TimeoutError, httpx.HTTPError, psycopg.Error, ValueError, TypeError, AttributeError, OverflowError):
            raise HTTPException(503, "PRODUCT_METADATA_UNAVAILABLE") from None

    async def image(self, ident):
        saved = None
        try:
            async with asyncio.timeout(5):
                saved = await self.lookup(ident)
                _, url, _ = saved
                if not url:
                    raise HTTPException(404, "PRODUCT_IMAGE_UNAVAILABLE")
                body, kind = await self.read(url, image=True)
                if not raster(body, kind):
                    raise HTTPException(503, "PRODUCT_IMAGE_INVALID")
                return body, kind
        except (HTTPException, TimeoutError, httpx.HTTPError, psycopg.Error, ValueError, TypeError, AttributeError, OverflowError) as exc:
            # A retry must obtain a fresh signed URL after image failure. Do not
            # discard a different request's newer entry or another product.
            if saved is not None and self.cache.get(ident) is saved:
                self.cache.pop(ident, None)
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(503, "PRODUCT_IMAGE_UNAVAILABLE") from None
