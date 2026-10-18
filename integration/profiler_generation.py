"""Bind canonical DB facts, immutable vector order and installed local query assets."""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "product-search"))
from catalog_db import projection, validate_generation

_spec = importlib.util.spec_from_file_location(
    "integration_encoder_contract", ROOT / "product-search/search/encoder_contract.py"
)
_encoder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_encoder)
encoder_contract = _encoder.encoder_contract
from profiling.embedding import active_data_dir
from profiling.ports import NoActiveCatalog


class DictDB:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, args=()):
        return self.connection.exec_driver_sql(sql, args).mappings()


class BoundGeneration:
    def __init__(self, root, engine):
        self.root = Path(root)
        self.engine = engine
        if not shutil.which("node"):
            raise RuntimeError("Node runtime is required")
        for name in [
            "service.mjs",
            "runtime.mjs",
            "model.lock.json",
            "node_modules",
            "models",
        ]:
            if not (self.root / "embedding" / name).exists():
                raise RuntimeError("Local encoder assets missing")
        self.check()
        manifest = json.loads(
            (active_data_dir(self.root) / "manifest.json").read_text()
        )
        if manifest.get("encoder_fingerprint") != encoder_contract(self.root, manifest):
            raise RuntimeError("Installed encoder/tokenizer fingerprint differs")
        self.expected_version = self.version
        paths = [
            self.root / "embedding" / n
            for n in ["service.mjs", "runtime.mjs", "documents.mjs", "model.lock.json"]
        ]
        paths.extend(
            p for p in (self.root / "embedding/models").iterdir() if p.is_file()
        )
        self.asset_stats = {
            p: (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns) for p in paths
        }

    def check(self):
        try:
            if any(
                (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns) != value
                for p, value in getattr(self, "asset_stats", {}).items()
            ):
                raise ValueError("Immutable encoder assets changed after validation")
            if hasattr(self, "execution_probe") and not self.execution_probe():
                raise ValueError("Required encoder process is not running")
            data = active_data_dir(self.root)
            manifest = json.loads((data / "manifest.json").read_text())
            catalog = json.loads((data / "catalog.json").read_text())
            vectors = (data / "vectors.f32").read_bytes()
            with self.engine.connect() as connection:
                connection.exec_driver_sql(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                db = DictDB(connection)
                if (
                    db.execute("SELECT version_num FROM alembic_version").fetchone()[
                        "version_num"
                    ]
                    != "0008"
                ):
                    raise ValueError(
                        "Integrated revision supports exactly migration0008"
                    )
                version = db.execute(
                    "SELECT * FROM ai_catalog.catalog_versions WHERE is_active"
                ).fetchone()
                if version is None:
                    raise ValueError("No active catalog")
                generation = db.execute(
                    "SELECT manifest,vectors FROM ai_catalog.search_generations WHERE catalog_version=%s",
                    (version["id"],),
                ).fetchone()
                if (
                    generation is None
                    or generation["manifest"] != manifest
                    or bytes(generation["vectors"]) != vectors
                ):
                    raise ValueError("DB and file generation differ")
                current = projection(db, version)
                raw = validate_generation(current, manifest, vectors, version["id"])
                if json.loads(raw) != catalog:
                    raise ValueError("File canonical catalog differs")
                if [p["id"] for p in current["products"]] != manifest["product_ids"]:
                    raise ValueError("Vector order differs")
                matrix = np.frombuffer(vectors, dtype=np.float32).reshape(
                    -1, manifest["dimensions"]
                )
                if not np.isfinite(matrix).all() or np.any(
                    np.linalg.norm(matrix, axis=1) == 0
                ):
                    raise ValueError("Non-finite or zero product vectors")
                if (
                    hasattr(self, "expected_version")
                    and str(version["id"]) != self.expected_version
                ):
                    raise ValueError(
                        "Loaded embedding cannot change catalog generations without a controlled restart"
                    )
                self.version = str(version["id"])
                self.count = len(current["products"])
                return self.version
        except Exception as error:
            raise NoActiveCatalog(
                "Integration DB/catalog/vector generation mismatch"
            ) from error


class GuardedCatalog:
    def __init__(self, catalog, binding):
        self.catalog = catalog
        self.binding = binding

    def __getattr__(self, name):
        return getattr(self.catalog, name)

    def active(self):
        expected = self.binding.check()
        version, products = self.catalog.active()
        if str(version) != expected:
            raise NoActiveCatalog("Profiler catalog cache version differs")
        return version, products


class GuardedEmbedding:
    def __init__(self, embedding, binding, state):
        self.embedding = embedding
        self.binding = binding
        self.state = state

    def __getattr__(self, name):
        return getattr(self.embedding, name)

    def _checked(self, method, *args):
        try:
            self.binding.check()
            result = getattr(self.embedding, method)(*args)
            if not np.isfinite(result).all() or (
                len(result) and np.any(np.linalg.norm(result, axis=1) == 0)
            ):
                raise RuntimeError("Required embedding returned invalid/zero vectors")
            return result
        except Exception:
            self.state.failure = "embedding_failed"
            raise RuntimeError(
                "Required embedding generation/execution failed"
            ) from None

    def product_vectors(self, ids):
        return self._checked("product_vectors", ids)

    def encode(self, texts):
        return self._checked("encode", texts)

    def similarities(self, ids, queries):
        """Guard Emmet's optional pgvector path as well as shared HTTP vectors."""
        try:
            self.binding.check()
            method = getattr(self.embedding, "similarities", None)
            result = method(ids, queries) if callable(method) else self.product_vectors(ids) @ queries.T
            if result.shape != (len(ids), len(queries)) or not np.isfinite(result).all():
                raise ValueError("Invalid similarity matrix")
            return result
        except Exception:
            self.state.failure = "embedding_failed"
            raise RuntimeError("Required embedding generation/execution failed") from None

    def close(self):
        self.embedding.close()
