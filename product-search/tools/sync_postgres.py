"""The integrated sync path: BE -> immutable PG payload -> validated cache -> pending.

Only the serving Search validates and activates pending generations. No paid model.
"""
import hashlib
import json
from pathlib import Path
import tempfile
from uuid import NAMESPACE_URL, uuid5

import numpy as np

from catalog_store import CatalogStore, Generation, materialize_generation
from search.complete_export import complete
from search.encoder_contract import encoder_contract, initial_manifest
from search.export_catalog import encode, project, utc
from search.product_tags import enrich, validate
from search.service import SearchService
from tools.sync_catalog import encode_documents


def sync(root, url, fetch, *, apply=False, embed=encode_documents, fingerprint=encoder_contract):
    root = Path(root)
    store = CatalogStore(url)
    with store.writer():
        state = store.state()
        if state["pending_id"]:
            raise RuntimeError("CATALOG_ACTIVATION_PENDING")
        previous = store.load(state["active_id"]) if state["active_id"] else None
        old = previous.catalog if previous else {"products": [], "taxonomy": {"categories": []}}
        manifest = dict(previous.manifest) if previous else initial_manifest(root)
        export, categories = fetch()
        export, export_id, source_digest = complete(export, categories)
        products_map = {p["source_product_id"]: p["id"] for p in old["products"] if p.get("source_product_id")}
        categories_map = {c["source_category_id"]: c["category_id"] for c in old["taxonomy"]["categories"] if c.get("source_category_id")}
        catalog = project(export, categories, old, products_map, categories_map)
        enrich(catalog)
        validate(catalog)
        lock = json.loads((root / "embedding/model.lock.json").read_text())
        signature = fingerprint(root, manifest)
        # A model change is an explicit drain/restart operation, never a hot swap.
        if previous and (manifest["encoder_fingerprint"] != signature or manifest.get("model_revision") != lock["revision"]):
            raise ValueError("ENCODER_CHANGE_REQUIRES_DRAIN_AND_RESTART")
        projection_digest = hashlib.sha256(encode(catalog)).hexdigest()
        identity = f"{export_id}:{source_digest}:{signature}:{projection_digest}"
        ident = str(uuid5(NAMESPACE_URL, identity))
        catalog["ai_catalog_version"] = ident
        old_indices = {p["id"]: i for i, p in enumerate(old["products"])}
        reuse_ok = bool(previous and old.get("document_contract") == catalog["document_contract"])
        changed, reused = [], []
        for i, p in enumerate(catalog["products"]):
            index = old_indices.get(p["id"])
            if reuse_ok and index is not None and old["products"][index]["document"] == p["document"]:
                reused.append((i, index))
            else:
                changed.append(i)
        result = {"catalogVersion": ident, "products": len(catalog["products"]),
                  "embedDocuments": len(changed), "reuseVectors": len(reused),
                  "removed": len(set(old_indices) - {p["id"] for p in catalog["products"]}),
                  "applied": apply, "activated": False}
        if not apply:
            return result
        store.confirm_export(export_id, source_digest, utc(export["data"]["generatedAt"]), [p["id"] for p in catalog["products"]])
        if state["active_id"] == ident:
            return result | {"activated": True, "state": "ALREADY_ACTIVE"}
        vectors = np.empty((len(catalog["products"]), 768), dtype=np.float32)
        counts = [0] * len(vectors)
        old_vectors = np.frombuffer(previous.vectors, dtype=np.float32).reshape(-1, 768) if previous else None
        for new, old_index in reused:
            vectors[new] = old_vectors[old_index]
            counts[new] = manifest["token_counts"][old_index]
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".embed-", dir=root) as temporary:
            new_vectors, new_counts = embed(root, [catalog["products"][i]["document"] for i in changed], Path(temporary))
        if np.shape(new_vectors) != (len(changed), 768) or len(new_counts) != len(changed):
            raise ValueError("INVALID_EMBEDDING_SHAPE")
        for index, vector, count in zip(changed, new_vectors, new_counts):
            if type(count) is not int or count < 1:
                raise ValueError("INVALID_TOKEN_COUNT")
            vectors[index], counts[index] = vector, count
        if not np.isfinite(vectors).all() or np.any(np.linalg.norm(vectors, axis=1) == 0):
            raise ValueError("INVALID_EMBEDDING")
        raw, vector_bytes = encode(catalog), vectors.tobytes()
        manifest.update(model_revision=lock["revision"], dimensions=768,
            encoder_fingerprint=signature, catalog_sha256=hashlib.sha256(raw).hexdigest(),
            vectors_sha256=hashlib.sha256(vector_bytes).hexdigest(),
            product_ids=[p["id"] for p in catalog["products"]],
            source_product_ids=[p["source_product_id"] for p in catalog["products"]],
            token_counts=counts, truncated_documents=sum(n > 1024 for n in counts), ai_catalog_version=ident)
        generation = Generation(ident, export_id, raw, manifest, vector_bytes)
        store.prepare(generation)
        directory = materialize_generation(generation, root)
        service = SearchService(directory)
        try:
            result["snapshotId"] = service.snapshot_id
        finally:
            service.close()
        store.propose(ident)
        result["state"] = "PENDING_SERVING_VALIDATION"
        return result
