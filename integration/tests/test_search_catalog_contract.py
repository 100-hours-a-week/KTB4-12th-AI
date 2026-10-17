"""Characterize the Profiler HTTP boundary with synthetic, in-process responses."""

import json

import httpx
import numpy as np
import pytest
from profiling.ports import NoActiveCatalog
from profiling.search_api import SearchCatalog


@pytest.fixture
def search_server():
    metadata = {
        "protocol": "shared-catalog/1",
        "snapshotId": "synthetic-generation",
        "catalogVersion": "00000000-0000-4000-8000-000000000001",
        "productCount": 501,
        "dimensions": 768,
        "distance": "cosine",
        "normalization": "l2",
        "model": "jinaai/jina-embeddings-v5-text-nano-retrieval",
        "modelSha256": "a" * 64,
        "encoderFingerprint": "b" * 64,
        "vectorsSha256": "c" * 64,
        "catalogSha256": "d" * 64,
        "precision": "q4f16",
        "maxTokens": 1024,
        "queryPrefix": "Query: ",
        "documentPrefix": "Document: ",
        "queryPreprocessing": "raw-text+encoder-prefix/truncate-1024/l2",
    }
    products = [
        {
            "productId": ident,
            "name": f"합성 상품 {ident}",
            "brand": "합성 브랜드",
            "description": None,
            "categoryId": 11,
            "categoryName": "합성 카테고리",
            "price": ident * 100,
            "updatedAt": "2026-10-16T00:00:00Z",
            "viewCount": ident,
        }
        for ident in range(1, 502)
    ]
    calls = []
    fault = {}

    def handle(request):
        path = request.url.path
        payload = json.loads(request.content) if request.content else None
        calls.append((path, dict(request.url.params), payload))
        if fault.get("http_error"):
            return httpx.Response(503, text="synthetic private response body")
        if path == "/v1/catalog/contract":
            return httpx.Response(200, json=metadata)
        if path == "/v1/catalog":
            offset = int(request.url.params["offset"])
            page = products[offset : offset + 500]
            if fault.get("page") == "short":
                page = page[:-1]
            if fault.get("page") == "duplicate" and offset:
                page = products[:1]
            reply = {**metadata, "products": page}
            if fault.get("page") == "generation":
                reply["snapshotId"] = "different-generation"
            return httpx.Response(200, json=reply)
        if path == "/v1/embeddings/products":
            ids = payload["ids"]
            vectors = np.eye(768, dtype=np.float32)[ids]
            reply = {"ids": ids, "vectors": vectors.tolist()}
            if fault.get("vectors") == "order":
                reply["ids"] = list(reversed(ids))
        elif path == "/v1/embeddings/query":
            vectors = np.eye(768, dtype=np.float32)[
                [int(t.split(":")[1]) for t in payload["texts"]]
            ]
            reply = {"vectors": vectors.tolist()}
        else:
            raise AssertionError(f"Unexpected synthetic request: {path}")
        reply["snapshotId"] = metadata["snapshotId"]
        if fault.get("vectors") == "generation":
            reply["snapshotId"] = "different-generation"
        elif fault.get("vectors") == "norm":
            reply["vectors"][0] = [0.0] * 768
        return httpx.Response(200, json=reply)

    with httpx.Client(
        base_url="http://synthetic", transport=httpx.MockTransport(handle)
    ) as client:
        yield client, calls, fault


def test_pagination_batch_order_raw_queries_and_empty_inputs(search_server):
    client, calls, _ = search_server
    catalog = SearchCatalog("http://synthetic", "synthetic", client=client)
    assert [product.productId for product in catalog.products] == list(range(1, 502))
    assert [params["offset"] for path, params, _ in calls if path == "/v1/catalog"] == [
        "0",
        "500",
    ]

    ids = list(range(501, 0, -1)) + [501]
    np.testing.assert_array_equal(catalog.product_vectors(ids), np.eye(768)[ids])
    batches = [
        body["ids"] for path, _, body in calls if path == "/v1/embeddings/products"
    ]
    assert [len(batch) for batch in batches] == [500, 2]
    assert batches[0] + batches[1] == ids

    texts = [f"  Synthetic TAG:{index}  " for index in range(65)]
    np.testing.assert_array_equal(catalog.encode(texts), np.eye(768)[:65])
    batches = [
        body["texts"] for path, _, body in calls if path == "/v1/embeddings/query"
    ]
    assert [len(batch) for batch in batches] == [32, 32, 1]
    assert [text for batch in batches for text in batch] == texts
    count = len(calls)
    for empty in (catalog.encode([]), catalog.product_vectors([])):
        assert empty.shape == (0, 768) and empty.dtype == np.float32
    assert len(calls) == count


@pytest.mark.parametrize("fault_name", ["short", "duplicate", "generation"])
def test_invalid_catalog_pages_close_initialization_client(search_server, fault_name):
    client, _, fault = search_server
    fault["page"] = fault_name
    with pytest.raises(ValueError):
        SearchCatalog("http://synthetic", "synthetic", client=client)
    assert client.is_closed


@pytest.mark.parametrize("fault_name", ["order", "generation", "norm"])
def test_product_vector_response_must_match_bound_generation_and_order(
    search_server, fault_name
):
    client, _, fault = search_server
    catalog = SearchCatalog("http://synthetic", "synthetic", client=client)
    fault["vectors"] = fault_name
    with pytest.raises(NoActiveCatalog):
        catalog.product_vectors([2, 1])


def test_request_failure_does_not_expose_response_body(search_server):
    client, _, fault = search_server
    catalog = SearchCatalog("http://synthetic", "synthetic", client=client)
    fault["http_error"] = True
    with pytest.raises(NoActiveCatalog, match="^Shared Search request failed$"):
        catalog.active()


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"snapshotId": "synthetic-generation"},
        {"snapshotId": "synthetic-generation", "vectors": [[1.0] * 768, [1.0]]},
        {
            "snapshotId": "synthetic-generation",
            "vectors": [["synthetic private"] * 768],
        },
        {"snapshotId": "synthetic-generation", "vectors": [[1e30] + [0.0] * 767]},
        {
            "snapshotId": "synthetic-generation",
            "vectors": [[float("nan")] + [0.0] * 767],
        },
        {"snapshotId": "synthetic-generation", "vectors": [[True] + [False] * 767]},
        None,
    ],
)
def test_malformed_vectors_have_one_redacted_exception_contract(search_server, reply):
    client, _, _ = search_server
    catalog = SearchCatalog("http://synthetic", "synthetic", client=client)
    with pytest.raises(
        NoActiveCatalog, match="^Invalid shared vector response$"
    ) as caught:
        catalog._matrix(reply, 1)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("body", [b"[]", b"null", b"synthetic private invalid JSON"])
def test_invalid_json_envelope_is_a_redacted_request_failure(body):
    with httpx.Client(
        base_url="http://synthetic",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body)
        ),
    ) as client:
        with pytest.raises(
            NoActiveCatalog, match="^Shared Search request failed$"
        ) as caught:
            SearchCatalog("http://synthetic", "synthetic", client=client)
        assert client.is_closed and caught.value.__suppress_context__
