"""Shared catalog/embedding protocol. Personal policy and profiles never live here."""

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from uuid import NAMESPACE_URL, UUID, uuid5

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field

from .catalog_bindings import BindingUnavailable
from .embedding import SearchBusy
from .models import Filters, Model, ProductId, SearchRequest
from .responses import APIErrorResponse
from .service import ALGORITHM
from .visibility import denied_products, visible

VECTOR_DIMENSIONS = 768
CATALOG_PAGE_SIZE = 500
QUERY_BATCH_SIZE = 32
QUERY_UTF16_LIMIT = 2000

# These routes enforce readiness after distinguishing current from retained
# snapshots. A DB publication must not invalidate an already accepted job.
CATALOG_PATHS = frozenset(
    {
        "/v1/catalog",
        "/v1/catalog/contract",
        "/v1/catalog/bindings",
        "/v1/embeddings/products",
        "/v1/embeddings/query",
        "/v1/vector-search",
    }
)


def manages_catalog_readiness(path):
    return path in CATALOG_PATHS or path.startswith("/v1/catalog/bindings/")


async def require_current_ready(request):
    check = getattr(request.app.state, "shared_catalog_readiness", None)
    if check is not None and not await check():
        raise HTTPException(503, "SEARCH_NOT_READY")


router = APIRouter(
    prefix="/v1",
    tags=["shared-catalog"],
    responses={status: {"model": APIErrorResponse} for status in (401, 409, 422, 503)},
)


class Bound(Model):
    snapshot_id: str = Field(min_length=1, max_length=80)
    binding_id: str | None = Field(default=None, min_length=1, max_length=128)


class Queries(Bound):
    texts: list[str] = Field(min_length=1, max_length=QUERY_BATCH_SIZE)


class Vectors(Bound):
    ids: list[ProductId] = Field(min_length=1, max_length=CATALOG_PAGE_SIZE)


class VectorSearch(Bound):
    vector: list[float] = Field(
        min_length=VECTOR_DIMENSIONS, max_length=VECTOR_DIMENSIONS
    )
    filters: Filters = Field(default_factory=Filters)
    limit: int = Field(default=30, ge=1, le=500, strict=True)


def unit_query_vector(values):
    # Scale in float64 before any squaring or float32 conversion. This accepts
    # every finite nonzero direction, including float64 subnormal magnitudes.
    vector = np.asarray(values, dtype=np.float64)
    if vector.shape != (VECTOR_DIMENSIONS,) or not np.isfinite(vector).all():
        raise ValueError("Invalid query vector")
    scale = np.max(np.abs(vector))
    if scale == 0:
        raise ValueError("Zero query vector")
    scaled = vector / scale
    unit = (scaled / np.linalg.norm(scaled)).astype(np.float32)
    norm = np.linalg.norm(unit.astype(np.float64))
    if (
        not np.isfinite(unit).all()
        or not np.isfinite(norm)
        or not np.isclose(norm, 1, atol=1e-6)
    ):
        raise ValueError("Invalid normalized query vector")
    return unit


def encoder_query_matrix(embeddings, count):
    """An encoder must return unit vectors; invalid output is never a success."""
    try:
        values = np.asarray([vector for vector, _ in embeddings])
        if values.dtype.kind not in "fi":
            raise ValueError("Non-numeric encoder output")
        matrix64 = values.astype(np.float64)
        with np.errstate(over="ignore", invalid="ignore"):
            norms = np.linalg.norm(matrix64, axis=1)
        if (
            matrix64.shape != (count, VECTOR_DIMENSIONS)
            or not np.isfinite(matrix64).all()
            or not np.allclose(norms, 1, atol=1e-5)
        ):
            raise ValueError("Invalid encoder shape or unit length")
        matrix = matrix64.astype(np.float32)
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
        if not np.isfinite(matrix).all() or not np.allclose(
            np.linalg.norm(matrix.astype(np.float64), axis=1), 1, atol=1e-5
        ):
            raise ValueError("Invalid normalized encoder output")
        return matrix
    except (TypeError, ValueError, OverflowError, IndexError):
        raise HTTPException(503, "INVALID_ENCODER_RESULT") from None


def contract(service):
    manifest = service.manifest
    catalog_version = service.catalog.get("ai_catalog_version") or str(
        uuid5(NAMESPACE_URL, service.snapshot_id)
    )
    UUID(catalog_version)
    return {
        "protocol": "shared-catalog/1",
        "snapshotId": service.snapshot_id,
        "catalogVersion": catalog_version,
        "productCount": len(visible(service.products)),
        "visibilityRevision": __import__('hashlib').sha256(','.join(map(str, sorted(denied_products.get()))).encode()).hexdigest(),
        "productTagsRule": service.catalog.get("product_tags_rule"),
        "productTagsSha256": service.catalog.get("product_tags_sha256"),
        "categoryTaxonomy": [{"id": c["category_id"], "name": c["category"], "parentId": c["parent_id"]}
                             for c in service.catalog["taxonomy"]["categories"]],
        "modelRevision": manifest.get("model_revision"),
        "model": manifest["model"],
        "modelSha256": manifest.get("model_sha256"),
        "encoderFingerprint": manifest.get("encoder_fingerprint"),
        "vectorsSha256": manifest["vectors_sha256"],
        "catalogSha256": manifest["catalog_sha256"],
        "dimensions": VECTOR_DIMENSIONS,
        "precision": manifest.get("precision"),
        "maxTokens": manifest["max_tokens"],
        "queryPrefix": manifest.get("query_prefix"),
        "documentPrefix": manifest.get("document_prefix"),
        "queryPreprocessing": "raw-text+encoder-prefix/truncate-1024/l2",
        "documentPreprocessing": service.catalog.get(
            "document_contract", "ai-catalog-db/1"
        ),
        "distance": "cosine",
        "normalization": "l2",
        "hybridAlgorithm": ALGORITHM,
    }


def authorize(request, *, required=False):
    token = os.getenv("SEARCH_SERVICE_TOKEN")
    if required and not token:
        raise HTTPException(503, "CATALOG_BINDING_UNAVAILABLE")
    if token and not secrets.compare_digest(
        request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()
    ):
        raise HTTPException(401, "UNAUTHORIZED")


@asynccontextmanager
async def pinned(request, snapshot=None, binding_id=None):
    """Authenticate and retain this service generation until the request finishes."""
    authorize(request, required=binding_id is not None)
    if binding_id is None:
        await require_current_ready(request)
    owner = request.app.state.search
    service = getattr(owner, "current", owner)
    if binding_id is not None:
        try:
            service = owner.bindings.resolve(binding_id, snapshot)
        except (AttributeError, BindingUnavailable):
            raise HTTPException(409, "CATALOG_BINDING_UNAVAILABLE") from None
    if snapshot is not None and snapshot != service.snapshot_id:
        raise HTTPException(409, "SNAPSHOT_MISMATCH")
    if hasattr(owner, "readers"):
        owner.readers[service] += 1
    try:
        yield service
    finally:
        if hasattr(owner, "readers"):
            await owner._release_reader(service)


def profile_product(product):
    # Missing popularity must not silently flatten Emmet's ranking.
    if "view_count" not in product or "source_updated_at" not in product:
        raise HTTPException(503, "CATALOG_PROFILE_FIELDS_MISSING_REPUBLISH_REQUIRED")
    return {
        "productId": product["id"],
        "name": product["name"],
        "brand": product["brand"],
        "description": product["description"] or None,
        "categoryId": product["category_id"],
        "categoryName": product["category"],
        "parentCategoryId": product["parent_category_id"],
        "parentCategoryName": product["category_group"],
        "productKind": product["kind"] or None,
        "price": product["price"],
        "availability": product["availability"],
        "updatedAt": product["source_updated_at"],
        "viewCount": product["view_count"],
        "tags": [[t["axis"], t["tag"]] for t in product["product_tags"]] if "product_tags" in product else None,
    }


@router.get("/catalog/contract")
async def metadata(
    request: Request, bindingId: str | None = Query(None, min_length=1, max_length=128)
):
    async with pinned(request, binding_id=bindingId) as service:
        return contract(service)


@router.post("/catalog/bindings")
async def retain_catalog(body: Bound, request: Request):
    authorize(request, required=True)
    await require_current_ready(request)
    owner = request.app.state.search
    try:
        ident, service = await owner.bindings.acquire(body.snapshot_id)
    except AttributeError:
        raise HTTPException(503, "CATALOG_BINDING_UNAVAILABLE") from None
    except BindingUnavailable as exc:
        status = 409 if str(exc) == "SNAPSHOT_MISMATCH" else 503
        raise HTTPException(status, str(exc)) from None
    try:
        metadata = contract(service)
    except Exception:
        await owner.bindings.release(ident)
        raise
    return {
        "bindingId": ident,
        "leaseSeconds": owner.bindings.LEASE_SECONDS,
        "contract": metadata,
    }


@router.delete("/catalog/bindings/{binding_id}")
async def release_catalog(binding_id: str, request: Request):
    authorize(request, required=True)
    owner = request.app.state.search
    if not hasattr(owner, "bindings"):
        raise HTTPException(503, "CATALOG_BINDING_UNAVAILABLE")
    await owner.bindings.release(binding_id)
    return {"released": True}


@router.get("/catalog")
async def catalog(
    request: Request,
    snapshotId: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(CATALOG_PAGE_SIZE, ge=1, le=CATALOG_PAGE_SIZE),
    bindingId: str | None = Query(None, min_length=1, max_length=128),
):
    async with pinned(request, snapshotId, bindingId) as service:
        return {
            **contract(service),
            "offset": offset,
            "products": [
                profile_product(product)
                for product in visible(service.products)[offset : offset + limit]
            ],
        }


@router.post("/embeddings/products")
async def products(body: Vectors, request: Request):
    async with pinned(request, body.snapshot_id, body.binding_id) as service:
        if any(product_id not in service.by_id or product_id in denied_products.get() for product_id in body.ids):
            raise HTTPException(422, "UNKNOWN_PRODUCT_ID")
        return {
            "snapshotId": service.snapshot_id,
            "ids": body.ids,
            "vectors": service.vectors[
                [service.by_id[product_id] for product_id in body.ids]
            ].tolist(),
        }


@router.post("/embeddings/query")
async def queries(body: Queries, request: Request):
    try:
        valid = all(
            text.strip() and len(text.encode("utf-16-le")) // 2 <= QUERY_UTF16_LIMIT
            for text in body.texts
        )
    except UnicodeError:
        valid = False
    if not valid:
        raise HTTPException(422, "INVALID_QUERY_TEXT")
    async with pinned(request, body.snapshot_id, body.binding_id) as service:
        if service.embedding is None:
            raise HTTPException(503, "ENCODER_UNAVAILABLE")
        # SharedEmbedding supplies bounded scheduling/single-flight. Do not
        # lowercase/normalize: Profiler previously encoded the exact tag text.
        try:
            embeddings = await asyncio.gather(
                *(service.embedding.get(text, "profile") for text in body.texts)
            )
        except SearchBusy as exc:
            raise HTTPException(
                503, "ENCODER_BUSY", headers={"Retry-After": "1"}
            ) from exc
        matrix = encoder_query_matrix(embeddings, len(body.texts))
        return {"snapshotId": service.snapshot_id, "vectors": matrix.tolist()}


@router.post("/vector-search")
async def vector_search(body: VectorSearch, request: Request):
    async with pinned(request, body.snapshot_id, body.binding_id) as service:
        try:
            vector = unit_query_vector(body.vector)
        except (ValueError, OverflowError) as exc:
            raise HTTPException(422, "INVALID_QUERY_VECTOR") from exc
        query = SearchRequest(filters=body.filters)
        try:
            service._validate(query)
        except ValueError as exc:
            raise HTTPException(422, "INVALID_FILTER") from exc
        scores = service.vectors @ vector
        indices = service._top_candidates(scores, service._eligible(query), body.limit)
        return {
            "snapshotId": service.snapshot_id,
            "distance": "cosine",
            "hits": [
                {"productId": service.products[i]["id"], "score": float(scores[i])}
                for i in indices
            ],
        }
