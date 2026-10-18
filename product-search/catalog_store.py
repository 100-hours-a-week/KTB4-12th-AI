"""Search-owned PostgreSQL generations. No serving-path BE fetches or migrations."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
import re
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from search.export_catalog import encode

WRITER_LOCK = 367109731502
SERVER_LOCK = 367109731503


@dataclass(frozen=True)
class Generation:
    id: str
    export_id: str
    catalog_json: bytes
    manifest: dict
    vectors: bytes

    @property
    def catalog(self):
        return json.loads(self.catalog_json)

    def validate(self):
        UUID(self.id)
        UUID(self.export_id)
        if self.catalog.get("ai_catalog_version") != self.id:
            raise ValueError("GENERATION_ID_MISMATCH")
        if hashlib.sha256(self.catalog_json).hexdigest() != self.manifest["catalog_sha256"]:
            raise ValueError("CATALOG_HASH_MISMATCH")
        if hashlib.sha256(self.vectors).hexdigest() != self.manifest["vectors_sha256"]:
            raise ValueError("VECTOR_HASH_MISMATCH")
        if self.manifest.get("dimensions") != 768:
            raise ValueError("VECTOR_DIMENSIONS_MISMATCH")
        for name in ("model_sha256", "encoder_fingerprint"):
            value = self.manifest.get(name)
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError("ENCODER_IDENTITY_MISSING")
        if not self.manifest.get("model_revision"):
            raise ValueError("MODEL_REVISION_MISSING")
        ids = [p["id"] for p in self.catalog["products"]]
        if ids != self.manifest["product_ids"] or len(ids) != len(set(ids)):
            raise ValueError("PRODUCT_ORDER_MISMATCH")


class CatalogStore:
    def __init__(self, url):
        self.url = url
        self._writer_connection = None

    def connect(self, **kwargs):
        return psycopg.connect(self.url, connect_timeout=3, row_factory=dict_row, **kwargs)

    @contextmanager
    def writer(self):
        # Session lock, no long transaction while fetching/encoding/building.
        with self.connect(autocommit=True) as db:
            if not db.execute("SELECT pg_try_advisory_lock(%s) AS held", (WRITER_LOCK,)).fetchone()["held"]:
                raise RuntimeError("CATALOG_SYNC_ALREADY_RUNNING")
            try:
                self._writer_connection = db
                yield
            finally:
                self._writer_connection = None
                if not db.closed and not db.broken:
                    db.execute("SELECT pg_advisory_unlock(%s)", (WRITER_LOCK,))

    @contextmanager
    def write_connection(self):
        db = self._writer_connection
        if db is None:
            raise RuntimeError("CATALOG_WRITER_LOCK_REQUIRED")
        # The transaction uses the lock-owning connection. Losing that session
        # cannot leave another connection writing after the writer lock vanished.
        with db.transaction():
            yield db

    def state(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM ai_catalog.catalog_state WHERE singleton").fetchone()
        if not row:
            raise RuntimeError("CATALOG_MIGRATION_REQUIRED")
        return {**row, "active_id": str(row["active_id"]) if row["active_id"] else None,
                "pending_id": str(row["pending_id"]) if row["pending_id"] else None}

    def load(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT * FROM ai_catalog.generations WHERE id=%s", (UUID(str(ident)),)).fetchone()
        if not row:
            raise ValueError("UNKNOWN_GENERATION")
        result = Generation(str(row["id"]), str(row["export_id"]), bytes(row["catalog_json"]), row["manifest"], bytes(row["vectors"]))
        result.validate()
        return result

    def denied(self):
        with self.connect() as db:
            rows = db.execute("SELECT product_id FROM ai_catalog.product_tombstones").fetchall()
        return frozenset(row["product_id"] for row in rows)

    def confirm_export(self, ident, digest, generated_at, product_ids):
        """Called only after complete-snapshot proof and all product/category validation.

        Deletion knowledge survives embedding/build failure and generation rollback.
        Reappearance never silently clears a tombstone; that needs a BE restore contract.
        """
        ident = UUID(ident)
        with self.write_connection() as db:
            previous = db.execute("SELECT * FROM ai_catalog.export_receipts ORDER BY generated_at DESC LIMIT 1").fetchone()
            if previous and generated_at < previous["generated_at"]:
                raise ValueError("STALE_EXPORT_RECEIPT")
            existing = db.execute("SELECT * FROM ai_catalog.export_receipts WHERE id=%s", (ident,)).fetchone()
            if existing:
                if existing["source_digest"].strip() != digest or existing["generated_at"] != generated_at or existing["product_ids"] != product_ids:
                    raise ValueError("EXPORT_ID_REUSED_WITH_DIFFERENT_CONTENT")
                return
            if previous and generated_at <= previous["generated_at"]:
                raise ValueError("STALE_OR_CONFLICTING_EXPORT")
            old_ids = previous["product_ids"] if previous else []
            if not previous:
                row = db.execute("SELECT g.catalog_json FROM ai_catalog.generations g JOIN ai_catalog.catalog_state s ON s.active_id=g.id WHERE s.singleton").fetchone()
                if row:
                    old_ids = [p["id"] for p in json.loads(bytes(row["catalog_json"]))["products"]]
            db.execute("INSERT INTO ai_catalog.export_receipts(id,source_digest,generated_at,product_ids) VALUES(%s,%s,%s,%s)",
                       (ident, digest, generated_at, product_ids))
            for removed in sorted(set(old_ids) - set(product_ids)):
                db.execute("INSERT INTO ai_catalog.product_tombstones(product_id,export_id) VALUES(%s,%s) ON CONFLICT DO NOTHING", (removed, ident))

    def prepare(self, generation):
        generation.validate()
        with self.write_connection() as db:
            row = db.execute("SELECT catalog_json,manifest,vectors FROM ai_catalog.generations WHERE id=%s", (UUID(generation.id),)).fetchone()
            if row:
                if bytes(row["catalog_json"]) != generation.catalog_json or row["manifest"] != generation.manifest or bytes(row["vectors"]) != generation.vectors:
                    raise ValueError("IMMUTABLE_GENERATION_CONFLICT")
            else:
                db.execute("INSERT INTO ai_catalog.generations(id,export_id,catalog_json,manifest,vectors) VALUES(%s,%s,%s,%s,%s)",
                           (UUID(generation.id), UUID(generation.export_id), generation.catalog_json, Jsonb(generation.manifest), generation.vectors))

    def propose(self, ident):
        with self.write_connection() as db:
            row = db.execute("SELECT active_id,pending_id FROM ai_catalog.catalog_state WHERE singleton FOR UPDATE").fetchone()
            if row["pending_id"] and str(row["pending_id"]) != ident:
                raise RuntimeError("CATALOG_ACTIVATION_PENDING")
            if row["active_id"] and str(row["active_id"]) == ident:
                return
            db.execute("UPDATE ai_catalog.catalog_state SET pending_id=%s,last_error=NULL WHERE singleton", (UUID(ident),))

    def activate(self, ident, expected_active, connection):
        """Only the single serving Search calls this after its own index validation."""
        # Use the serving lock's own session. If PG drops it, this commit cannot
        # continue on an unrelated new connection after another server takes over.
        db = connection
        with db.transaction():
            row = db.execute("SELECT active_id,pending_id FROM ai_catalog.catalog_state WHERE singleton FOR UPDATE").fetchone()
            active = str(row["active_id"]) if row["active_id"] else None
            if active != expected_active or str(row["pending_id"]) != ident:
                raise RuntimeError("CATALOG_ACTIVATION_CONFLICT")
            db.execute("UPDATE ai_catalog.catalog_state SET active_id=%s,pending_id=NULL,revision=revision+1,last_error=NULL,activated_at=now() WHERE singleton", (UUID(ident),))

    def reject(self, ident, connection):
        db = connection
        with db.transaction():
            db.execute("UPDATE ai_catalog.catalog_state SET pending_id=NULL,last_error='GENERATION_VALIDATION_FAILED' WHERE singleton AND pending_id=%s", (UUID(ident),))


def materialize_generation(generation, root):
    """Rebuild only derived files; corrupt/missing cache never changes PG."""
    generation.validate()
    target = Path(root) / "data" / "generations" / UUID(generation.id).hex
    target.mkdir(parents=True, exist_ok=True)
    for name, content in {"catalog.json": generation.catalog_json, "manifest.json": encode(generation.manifest), "vectors.f32": generation.vectors}.items():
        path = target / name
        if path.is_file() and path.read_bytes() == content:
            continue
        fd, temporary = tempfile.mkstemp(prefix=".restore-", dir=target)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return target
