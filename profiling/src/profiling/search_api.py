"""CatalogReader + EmbeddingIndex over Search; Emmet owns selection and profiles."""

import re
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import numpy as np

from profiling.ports import NoActiveCatalog
from profiling.schemas import ProductRecord

VECTOR_DIMENSIONS = 768
CATALOG_PAGE_SIZE = 500
QUERY_BATCH_SIZE = 32


def _validate_contract(metadata):
    """Reject incompatible assets before loading any products or querying vectors."""
    if (
        metadata["protocol"] != "shared-catalog/1"
        or metadata["dimensions"] != VECTOR_DIMENSIONS
        or metadata["distance"] != "cosine"
        or metadata["normalization"] != "l2"
    ):
        raise ValueError("Unsupported shared embedding contract")
    for field in (
        "modelSha256",
        "encoderFingerprint",
        "vectorsSha256",
        "catalogSha256",
    ):
        digest = metadata.get(field)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Unversioned shared embedding assets")
    if (
        metadata.get("model") != "jinaai/jina-embeddings-v5-text-nano-retrieval"
        or metadata.get("precision") != "q4f16"
        or metadata.get("maxTokens") != 1024
        or metadata.get("queryPrefix") != "Query: "
        or metadata.get("documentPrefix") != "Document: "
        or metadata.get("queryPreprocessing")
        != "raw-text+encoder-prefix/truncate-1024/l2"
    ):
        raise ValueError("Incompatible encoder/preprocessing")
    if metadata.get("productTagsRule") is not None:
        from profiling import vocab
        if metadata.get("productTagsRule") != vocab.RULE or metadata.get("productTagsSha256") != vocab.tags_sha256():
            raise ValueError("Product tag vocabulary differs from Profiler policy")
    if metadata.get("modelRevision") not in (None, "ac5d898c8d382b17167c33e5c8af644a3519b47d"):
        raise ValueError("Unapproved shared encoder revision")


class SearchCatalog:
    """Bind one Search generation; never interpret preferences or rerank products."""

    def __init__(self, base_url, token, *, client=None, retain_generation=False):
        parsed_url = urlsplit(base_url)
        if (
            parsed_url.scheme not in ("http", "https")
            or not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("Explicit internal Search URL required")
        self.client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=20,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
            follow_redirects=False,
        )
        self.binding_id = None
        self._closed = False
        try:
            self.contract = self._request("GET", "/v1/catalog/contract")
            if retain_generation:
                binding = self._request(
                    "POST",
                    "/v1/catalog/bindings",
                    json={"snapshotId": self.contract["snapshotId"]},
                )
                ident = binding.get("bindingId")
                if not isinstance(ident, str) or not re.fullmatch(
                    r"[0-9a-f]{32}", ident
                ):
                    raise NoActiveCatalog("Invalid shared catalog binding")
                self.binding_id = ident
                if (
                    binding.get("leaseSeconds") != 600
                    or binding.get("contract") != self.contract
                ):
                    raise NoActiveCatalog("Invalid shared catalog binding")
            _validate_contract(self.contract)
            self.snapshot = self.contract["snapshotId"]
            self.catalog_version = UUID(self.contract["catalogVersion"])
            self.version = (
                f"search-api/1:{self.snapshot}:"
                f"{self.contract['encoderFingerprint']}:{self.contract['vectorsSha256']}"
            )
            self.products = self._load_products()
            self.by_product = {product.productId: product for product in self.products}
            product_count = self.contract["productCount"]
            if len(self.by_product) != product_count:
                raise ValueError("Duplicate catalog products")
            self.matched = self.total = product_count
            self.check()
        except Exception:
            self.close()
            raise

    def _load_products(self) -> list[ProductRecord]:
        product_count = self.contract["productCount"]
        if type(product_count) is not int or not 0 <= product_count <= 1_000_000:
            raise ValueError("Invalid product count")
        products = []
        for offset in range(0, product_count, CATALOG_PAGE_SIZE):
            page = self._request(
                "GET",
                "/v1/catalog",
                params=self._bound(
                    {
                        "snapshotId": self.snapshot,
                        "offset": offset,
                        "limit": CATALOG_PAGE_SIZE,
                    }
                ),
            )
            page_contract = {field: page[field] for field in self.contract}
            expected_count = min(CATALOG_PAGE_SIZE, product_count - offset)
            if (
                page_contract != self.contract
                or len(page["products"]) != expected_count
            ):
                raise ValueError("Mixed or incomplete catalog pages")
            products.extend(
                ProductRecord.model_validate(product) for product in page["products"]
            )
        return products

    def _request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise TypeError("Expected shared response object")
            return result
        except Exception:  # noqa: BLE001 — redact transport and response parser failures at this boundary.
            # Do not expose private tag query content or server response bodies.
            raise NoActiveCatalog("Shared Search request failed") from None

    def _bound(self, payload):
        return {**payload, "bindingId": self.binding_id} if self.binding_id else payload

    def check(self):
        if (
            self._request("GET", "/v1/catalog/contract", params=self._bound({}))
            != self.contract
        ):
            raise NoActiveCatalog(
                "Shared Search generation changed; restart Profiler to bind it"
            )
        return str(self.catalog_version)

    def active(self):
        self.check()
        return self.catalog_version, list(self.products)

    def by_id(self, ident):
        self.check()
        return self.by_product.get(ident)

    def validate_disliked_categories(self, categories):
        if not categories:
            return
        self.check()
        taxonomy = self.contract.get("categoryTaxonomy")
        if not isinstance(taxonomy, list) or any(
            not isinstance(c, dict) or type(c.get("id")) is not int
            or not isinstance(c.get("name"), str) or not c["name"]
            for c in taxonomy
        ):
            raise NoActiveCatalog("CATEGORY_TAXONOMY_UNAVAILABLE")
        ids, names = {c["id"] for c in taxonomy}, {c["name"] for c in taxonomy}
        # Preserve Emmet's ID-or-name mapping, but reject an unknown mapping.
        # A valid category with zero products remains a valid exclusion.
        if any(c.category_id not in ids and c.category_name not in names for c in categories):
            raise NoActiveCatalog("UNKNOWN_EXPLICIT_DISLIKE_CATEGORY")

    def _matrix(self, reply, count):
        """Validate the generation, shape and unit length before Emmet sees vectors."""
        try:
            values = np.asarray(reply["vectors"])
            if values.dtype.kind not in "fi":
                raise ValueError("Non-numeric vectors")
            matrix = values.astype(np.float64)
            if (
                reply["snapshotId"] != self.snapshot
                or matrix.shape != (count, VECTOR_DIMENSIONS)
                or not np.isfinite(matrix).all()
            ):
                raise ValueError("Invalid vector shape or generation")
            with np.errstate(over="ignore", invalid="ignore"):
                norms = np.linalg.norm(matrix, axis=1)
            if not np.allclose(norms, 1, atol=1e-5):
                raise ValueError("Non-unit vectors")
            return matrix.astype(np.float32)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise NoActiveCatalog("Invalid shared vector response") from None

    def product_vectors(self, ids):
        matrices = []
        for start in range(0, len(ids), CATALOG_PAGE_SIZE):
            batch = ids[start : start + CATALOG_PAGE_SIZE]
            reply = self._request(
                "POST",
                "/v1/embeddings/products",
                json=self._bound({"snapshotId": self.snapshot, "ids": batch}),
            )
            if reply.get("ids") != batch:
                raise NoActiveCatalog("Product vector order mismatch")
            matrices.append(self._matrix(reply, len(batch)))
        return (
            np.concatenate(matrices)
            if matrices
            else np.empty((0, VECTOR_DIMENSIONS), dtype=np.float32)
        )

    def encode(self, texts):
        matrices = []
        for start in range(0, len(texts), QUERY_BATCH_SIZE):
            batch = texts[start : start + QUERY_BATCH_SIZE]
            reply = self._request(
                "POST",
                "/v1/embeddings/query",
                json=self._bound({"snapshotId": self.snapshot, "texts": batch}),
            )
            matrices.append(self._matrix(reply, len(batch)))
        return (
            np.concatenate(matrices)
            if matrices
            else np.empty((0, VECTOR_DIMENSIONS), dtype=np.float32)
        )

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.binding_id:
                try:
                    self._request("DELETE", "/v1/catalog/bindings/" + self.binding_id)
                except NoActiveCatalog:
                    # A failed release expires on Search; never expose the response.
                    pass
        finally:
            self.client.close()
