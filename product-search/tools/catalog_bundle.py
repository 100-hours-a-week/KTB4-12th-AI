"""Embedding bundle: embed once on the AI computer, import in an ECS one-off task.

    write:   python product-search/tools/catalog_bundle.py --write-bundle catalog-bundle.tar
    import:  python product-search/tools/catalog_bundle.py [--bundle-s3 s3://<bucket>/<key> | --bundle-file <tar>] [--apply]

The AI computer loads the catalog with integration.search_sync as usual; once the
serving Search has validated and activated it, --write-bundle writes that active
generation (catalog, vectors, product tags, export receipt) as one tar. The one-off
task imports it without embedding: hashes, encoder identity and the export receipt are
checked before anything is written, and the serving Search still validates and
activates the pending generation (integration.search_sync's flow, unchanged).

Without a source argument the S3 object comes from CATALOG_BUNDLE_S3 (Parameter
Store), so the task definition stays fixed while the AI team points it at a new
upload. Both the dry run and --apply report the DB's active generation and refuse a
bundle older than it unless --allow-older (an explicit rollback).
"""
import argparse
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "product-search"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from integration.containers.entrypoint import prepare_encoder, secret  # noqa: E402
from integration.database_identity import audit, validate_dsn  # noqa: E402

MAX_BUNDLE_BYTES = 256 * 1024 * 1024
BUNDLE_FORMAT = "emmet-catalog-bundle/1"
BUNDLE_MEMBERS = ("bundle.json", "manifest.json", "catalog.json", "vectors.f32")


def s3_location(value):
    u = urlsplit(value)
    key = u.path.lstrip("/")
    if (u.scheme != "s3" or not u.hostname or u.username is not None or u.password is not None
            or u.port is not None or u.query or u.fragment or not key or key.endswith("/")):
        raise ValueError("S3_OBJECT_URI_REQUIRED")
    return u.hostname, key


def s3_client():
    import boto3
    return boto3.client("s3")  # credentials: ECS task role (or the operator's AWS profile)


def read_s3_object(uri, limit, client):
    bucket, key = s3_location(uri)
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        raw = body.read(limit + 1)
    finally:
        body.close()
    if len(raw) > limit:
        raise ValueError("BUNDLE_TOO_LARGE")
    return raw


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def write_bundle(generation, receipt):
    """One deterministic tar: receipt + generation, nothing derived from the local machine."""
    generation.validate()
    if receipt["id"] != generation.export_id or receipt["product_ids"] != generation.manifest["product_ids"]:
        raise ValueError("BUNDLE_RECEIPT_MISMATCH")
    meta = {"format": BUNDLE_FORMAT, "generation_id": generation.id, "export_id": generation.export_id,
            "receipt": {"source_digest": receipt["source_digest"], "generated_at": receipt["generated_at"]}}
    content = {"bundle.json": _canonical(meta), "manifest.json": _canonical(generation.manifest),
               "catalog.json": generation.catalog_json, "vectors.f32": generation.vectors}
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in BUNDLE_MEMBERS:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(content[name]), 0o644, 0
            tar.addfile(info, io.BytesIO(content[name]))
    return out.getvalue()


def read_bundle(raw, generation_type):
    """Read members into memory only (never extract to disk); exact member set."""
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError("BUNDLE_TOO_LARGE")
    content = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        members = tar.getmembers()
        if sorted(m.name for m in members) != sorted(BUNDLE_MEMBERS) or not all(m.isfile() for m in members):
            raise ValueError("BUNDLE_MEMBERS_INVALID")
        for member in members:
            content[member.name] = tar.extractfile(member).read()
    meta = json.loads(content["bundle.json"])
    if meta.get("format") != BUNDLE_FORMAT:
        raise ValueError("BUNDLE_FORMAT_UNSUPPORTED")
    manifest = json.loads(content["manifest.json"])
    generation = generation_type(meta["generation_id"], meta["export_id"], content["catalog.json"], manifest, content["vectors.f32"])
    generation.validate()
    receipt = {"id": meta["export_id"], "source_digest": meta["receipt"]["source_digest"],
               "generated_at": meta["receipt"]["generated_at"], "product_ids": manifest["product_ids"]}
    return generation, receipt


def check_encoder(root, generation, fingerprint):
    """The bundle must come from the same model files and encoder code as this image."""
    lock = json.loads((root / "embedding/model.lock.json").read_text())
    if generation.manifest.get("model_revision") != lock["revision"]:
        raise ValueError("BUNDLE_MODEL_REVISION_MISMATCH")
    if fingerprint(root, generation.manifest) != generation.manifest["encoder_fingerprint"]:
        raise ValueError("BUNDLE_ENCODER_MISMATCH")


def receipt_generated_at(store, generation_id):
    """When the export behind a stored generation was made (None if no receipt)."""
    with store.connect() as db:
        row = db.execute("""SELECT r.generated_at FROM ai_catalog.generations g
            JOIN ai_catalog.export_receipts r ON r.id = g.export_id WHERE g.id = %s""", (generation_id,)).fetchone()
    return row["generated_at"] if row else None


def compare_with_db(store, state, generation, receipt, allow_older):
    """The DB's current catalog next to the bundle; an older bundle is a rollback."""
    active = state["active_id"]
    status = {"alreadyActive": active == generation.id, "activeCatalogVersion": active,
              "pendingCatalogVersion": state["pending_id"], "bundleGeneratedAt": receipt["generated_at"],
              "activeGeneratedAt": None}
    if active and active != generation.id:
        active_time = receipt_generated_at(store, active)
        if active_time is not None:
            status["activeGeneratedAt"] = active_time.isoformat()
            if datetime.fromisoformat(receipt["generated_at"]) < active_time and not allow_older:
                raise ValueError("BUNDLE_OLDER_THAN_ACTIVE")
    return status


def import_bundle(root, store, raw, *, apply=False, allow_older=False, generation_type, fingerprint):
    generation, receipt = read_bundle(raw, generation_type)
    check_encoder(root, generation, fingerprint)
    result = {"catalogVersion": generation.id, "products": len(generation.manifest["product_ids"]),
              "embedDocuments": 0, "fromBundle": True, "applied": apply, "activated": False}
    if not apply:
        return result | compare_with_db(store, store.state(), generation, receipt, allow_older)
    with store.writer():
        state = store.state()
        result |= compare_with_db(store, state, generation, receipt, allow_older)
        if result["alreadyActive"]:
            return result | {"activated": True, "state": "ALREADY_ACTIVE"}
        if state["active_id"]:
            active = store.load(state["active_id"]).manifest
            if active.get("encoder_fingerprint") != generation.manifest["encoder_fingerprint"]:
                raise ValueError("ENCODER_CHANGE_REQUIRES_DRAIN_AND_RESTART")
        store.confirm_export(receipt["id"], receipt["source_digest"],
                             datetime.fromisoformat(receipt["generated_at"]), receipt["product_ids"])
        store.prepare(generation)
        store.propose(generation.id)
    return result | {"state": "PENDING_SERVING_VALIDATION"}


def export_bundle(store):
    """Active (already Search-validated) generation + its export receipt from this DB."""
    state = store.state()
    if not state["active_id"] or state["pending_id"]:
        raise ValueError("BUNDLE_NEEDS_ONE_ACTIVE_GENERATION")
    generation = store.load(state["active_id"])
    with store.connect() as db:
        row = db.execute("SELECT * FROM ai_catalog.export_receipts WHERE id=%s", (generation.export_id,)).fetchone()
    if not row:
        raise ValueError("BUNDLE_RECEIPT_MISSING")
    receipt = {"id": str(row["id"]), "source_digest": row["source_digest"].strip(),
               "generated_at": row["generated_at"].isoformat(), "product_ids": list(row["product_ids"])}
    return write_bundle(generation, receipt), generation


def parse_args(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--bundle-s3", help="import a bundle from S3 (default: CATALOG_BUNDLE_S3)")
    source.add_argument("--bundle-file", type=Path, help="import a bundle from a local file")
    source.add_argument("--write-bundle", type=Path, help="write this DB's active generation as a bundle")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-older", action="store_true", help="accept a bundle older than the active catalog (rollback)")
    args = parser.parse_args(argv)
    if not (args.bundle_s3 or args.bundle_file or args.write_bundle):
        args.bundle_s3 = environ.get("CATALOG_BUNDLE_S3", "").strip() or None
        if not args.bundle_s3:
            parser.error("one source is required (or CATALOG_BUNDLE_S3)")
    if args.write_bundle and (args.apply or args.allow_older):
        parser.error("--write-bundle only reads the DB")
    return args


def main(argv=None):
    args = parse_args(argv)
    database = os.environ["AI_DATABASE_NAME"]
    url = validate_dsn(secret("CATALOG_SYNC_DATABASE_URL"), database, "ai_catalog_sync")
    audit(url, database)
    from catalog_store import CatalogStore, Generation
    from search.encoder_contract import encoder_contract
    store = CatalogStore(url)
    if args.write_bundle:
        raw, generation = export_bundle(store)
        args.write_bundle.write_bytes(raw)
        print(json.dumps({"bundle": str(args.write_bundle), "bytes": len(raw), "catalogVersion": generation.id,
                          "products": len(generation.manifest["product_ids"])}, ensure_ascii=False, indent=2))
        return
    root = Path(os.environ.get("SEARCH_RUNTIME_ROOT", "/runtime"))
    prepare_encoder(root, Path(os.environ.get("ASSET_ROOT", "/assets")))
    if args.bundle_s3:
        raw = read_s3_object(args.bundle_s3, MAX_BUNDLE_BYTES, s3_client())
    else:
        if args.bundle_file.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError("BUNDLE_TOO_LARGE")
        raw = args.bundle_file.read_bytes()
    result = import_bundle(root, store, raw, apply=args.apply, allow_older=args.allow_older,
                           generation_type=Generation, fingerprint=encoder_contract)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def failure_line(exc):
    """Only fixed error codes (e.g. BUNDLE_OLDER_THAN_ACTIVE) are printed; free text may hold a DSN."""
    code = str(exc) if re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", str(exc)) else ""
    return f"Catalog bundle failed ({type(exc).__name__}{': ' + code if code else ''}); inspect PG state before retrying."


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(failure_line(exc), file=sys.stderr)
        raise SystemExit(1) from None
