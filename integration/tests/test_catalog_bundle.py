"""Embedding bundle (product-search/tools/catalog_bundle.py) — write once, import from S3. No network."""

from contextlib import contextmanager
import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("catalog_bundle", ROOT / "product-search/tools/catalog_bundle.py")
bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bundle)

# Fake credentials are assembled in pieces so secret scanners do not flag them.
FAKE_PW = "pw" + "123"


class FakeS3:
    def __init__(self, objects):
        self.objects, self.calls = objects, []

    def get_object(self, Bucket, Key):
        self.calls.append((Bucket, Key))
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def test_s3_location_accepts_only_plain_object_uris():
    assert bundle.s3_location("s3://ai-catalog/catalog/2026-10-25/catalog-bundle.tar") == (
        "ai-catalog", "catalog/2026-10-25/catalog-bundle.tar")
    for bad in ["https://ai-catalog/catalog/b.tar", "s3://ai-catalog/", "s3://ai-catalog",
                "s3://ai-catalog/catalog/", "s3://ai-catalog/b.tar?versionId=1",
                "s3://user" + ":" + FAKE_PW + "@ai-catalog/b.tar", "s3://ai-catalog:9000/b.tar", "s3:///b.tar"]:
        with pytest.raises(ValueError):
            bundle.s3_location(bad)


GEN_ID = "11111111-1111-5111-8111-111111111111"
EXPORT_ID = "22222222-2222-4222-8222-222222222222"
FINGERPRINT = "f" * 64


def make_generation(vectors=b"\x00" * 8, fingerprint=FINGERPRINT, revision="rev-1"):
    import hashlib
    from catalog_store import Generation

    catalog = json.dumps({"ai_catalog_version": GEN_ID, "products": [{"id": 7}, {"id": 9}]}).encode()
    manifest = {"catalog_sha256": hashlib.sha256(catalog).hexdigest(), "vectors_sha256": hashlib.sha256(vectors).hexdigest(),
                "dimensions": 768, "model_sha256": "a" * 64, "encoder_fingerprint": fingerprint,
                "model_revision": revision, "product_ids": [7, 9]}
    return Generation(GEN_ID, EXPORT_ID, catalog, manifest, vectors)


RECEIPT = {"id": EXPORT_ID, "source_digest": "d" * 64, "generated_at": "2026-10-19T05:00:00+00:00", "product_ids": [7, 9]}


class FakeStore:
    def __init__(self, active=None, active_manifest=None):
        self.active, self.active_manifest, self.calls = active, active_manifest, []

    @contextmanager
    def writer(self):
        self.calls.append("lock")
        yield

    def state(self):
        return {"active_id": self.active, "pending_id": None}

    def load(self, ident):
        return type("G", (), {"manifest": self.active_manifest})()

    def confirm_export(self, ident, digest, generated_at, product_ids):
        self.calls.append(("confirm", ident, digest, generated_at.isoformat(), product_ids))

    def prepare(self, generation):
        self.calls.append(("prepare", generation.id))

    def propose(self, ident):
        self.calls.append(("propose", ident))


def encoder_root(tmp_path, revision="rev-1"):
    (tmp_path / "embedding").mkdir()
    (tmp_path / "embedding/model.lock.json").write_text(json.dumps({"revision": revision}))
    return tmp_path


def same_encoder(root, manifest):
    return FINGERPRINT


def test_bundle_round_trip_is_deterministic_and_complete():
    from catalog_store import Generation

    raw = bundle.write_bundle(make_generation(), RECEIPT)
    assert raw == bundle.write_bundle(make_generation(), RECEIPT)
    generation, receipt = bundle.read_bundle(raw, Generation)
    assert generation == make_generation() and receipt == RECEIPT


def test_bundle_rejects_tampering_and_unexpected_members():
    import tarfile
    from catalog_store import Generation

    raw = bytearray(bundle.write_bundle(make_generation(vectors=b"\x01" * 8), RECEIPT))
    raw[raw.rindex(b"\x01" * 8)] = 2
    with pytest.raises(ValueError, match="VECTOR_HASH_MISMATCH"):
        bundle.read_bundle(bytes(raw), Generation)
    extra = io.BytesIO()
    with tarfile.open(fileobj=extra, mode="w") as tar:
        info = tarfile.TarInfo("../escape.json")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))
    with pytest.raises(ValueError, match="BUNDLE_MEMBERS_INVALID"):
        bundle.read_bundle(extra.getvalue(), Generation)
    with pytest.raises(ValueError, match="BUNDLE_RECEIPT_MISMATCH"):
        bundle.write_bundle(make_generation(), {**RECEIPT, "product_ids": [7]})


def test_bundle_dry_run_checks_encoder_and_writes_nothing(tmp_path):
    from catalog_store import Generation

    raw = bundle.write_bundle(make_generation(), RECEIPT)
    store = FakeStore()
    result = bundle.import_bundle(encoder_root(tmp_path), store, raw, generation_type=Generation, fingerprint=same_encoder)
    assert result["products"] == 2 and result["embedDocuments"] == 0 and result["applied"] is False
    assert store.calls == []
    with pytest.raises(ValueError, match="BUNDLE_ENCODER_MISMATCH"):
        bundle.import_bundle(tmp_path, store, raw, generation_type=Generation, fingerprint=lambda r, m: "0" * 64)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="BUNDLE_MODEL_REVISION_MISMATCH"):
        bundle.import_bundle(encoder_root(other, "rev-2"), store, raw, generation_type=Generation, fingerprint=same_encoder)


def test_bundle_apply_records_receipt_then_proposes_for_serving_validation(tmp_path, monkeypatch):
    from catalog_store import Generation

    monkeypatch.setattr(bundle, "receipt_generated_at", lambda store, ident: None)

    raw = bundle.write_bundle(make_generation(), RECEIPT)
    root = encoder_root(tmp_path)
    store = FakeStore()
    result = bundle.import_bundle(root, store, raw, apply=True, generation_type=Generation, fingerprint=same_encoder)
    assert result["state"] == "PENDING_SERVING_VALIDATION"
    assert store.calls == ["lock", ("confirm", EXPORT_ID, "d" * 64, RECEIPT["generated_at"], [7, 9]),
                           ("prepare", GEN_ID), ("propose", GEN_ID)]
    assert bundle.import_bundle(root, FakeStore(active=GEN_ID), raw, apply=True, generation_type=Generation,
                                     fingerprint=same_encoder)["state"] == "ALREADY_ACTIVE"
    changed = FakeStore(active="33333333-3333-4333-8333-333333333333", active_manifest={"encoder_fingerprint": "e" * 64})
    with pytest.raises(ValueError, match="ENCODER_CHANGE_REQUIRES_DRAIN_AND_RESTART"):
        bundle.import_bundle(root, changed, raw, apply=True, generation_type=Generation, fingerprint=same_encoder)
    assert ("prepare", GEN_ID) not in changed.calls


def test_bundle_reports_db_state_on_dry_run_and_refuses_older_bundles(tmp_path, monkeypatch):
    from datetime import datetime
    from catalog_store import Generation

    raw = bundle.write_bundle(make_generation(), RECEIPT)
    root = encoder_root(tmp_path)
    other = "33333333-3333-4333-8333-333333333333"
    times = {other: datetime.fromisoformat("2026-10-19T04:00:00+00:00")}
    monkeypatch.setattr(bundle, "receipt_generated_at", lambda store, ident: times.get(ident))

    dry = bundle.import_bundle(root, FakeStore(active=GEN_ID), raw, generation_type=Generation, fingerprint=same_encoder)
    assert dry["alreadyActive"] is True and dry["applied"] is False
    dry = bundle.import_bundle(root, FakeStore(active=other), raw, generation_type=Generation, fingerprint=same_encoder)
    assert dry["alreadyActive"] is False and dry["activeGeneratedAt"] == "2026-10-19T04:00:00+00:00"
    assert bundle.import_bundle(root, FakeStore(), raw, generation_type=Generation,
                                     fingerprint=same_encoder)["activeCatalogVersion"] is None

    times[other] = datetime.fromisoformat("2026-10-19T06:00:00+00:00")  # active is newer than the bundle
    for apply in (False, True):
        store = FakeStore(active=other, active_manifest={"encoder_fingerprint": FINGERPRINT})
        with pytest.raises(ValueError, match="BUNDLE_OLDER_THAN_ACTIVE"):
            bundle.import_bundle(root, store, raw, apply=apply, generation_type=Generation, fingerprint=same_encoder)
        assert ("prepare", GEN_ID) not in store.calls
    store = FakeStore(active=other, active_manifest={"encoder_fingerprint": FINGERPRINT})
    result = bundle.import_bundle(root, store, raw, apply=True, allow_older=True,
                                       generation_type=Generation, fingerprint=same_encoder)
    assert result["state"] == "PENDING_SERVING_VALIDATION" and ("propose", GEN_ID) in store.calls


def test_bundle_s3_path_can_come_from_the_environment():
    env = {"CATALOG_BUNDLE_S3": "s3://ai-catalog/catalog/d/catalog-bundle.tar"}
    args = bundle.parse_args(["--apply"], env)
    assert args.bundle_s3 == env["CATALOG_BUNDLE_S3"] and args.apply is True
    args = bundle.parse_args(["--bundle-file", "b.tar"], env)  # an explicit source wins
    assert args.bundle_file.name == "b.tar" and args.bundle_s3 is None
    with pytest.raises(SystemExit) as stopped:
        bundle.parse_args([], {})
    assert stopped.value.code == 2


@pytest.mark.parametrize("argv", [
    ["--bundle-s3", "s3://a/b.tar", "--bundle-file", "b.tar"],
    ["--write-bundle", "b.tar", "--apply"],
    ["--write-bundle", "b.tar", "--allow-older"],
    ["--export-file", "p.json"],
])
def test_bundle_cli_rejects_mixed_or_foreign_options(argv):
    with pytest.raises(SystemExit) as stopped:
        bundle.parse_args(argv, {})
    assert stopped.value.code == 2


def test_failure_line_shows_fixed_codes_but_never_free_text():
    assert "BUNDLE_OLDER_THAN_ACTIVE" in bundle.failure_line(ValueError("BUNDLE_OLDER_THAN_ACTIVE"))
    leaked = bundle.failure_line(ValueError("could not connect to postgresql://u:" + FAKE_PW + "@h/db"))
    assert FAKE_PW not in leaked and leaked.startswith("Catalog bundle failed (ValueError)")


def test_bundle_s3_read_is_size_limited():
    s3 = FakeS3({("ai-catalog", "catalog/d/catalog-bundle.tar"): b"x" * 9})
    assert bundle.read_s3_object("s3://ai-catalog/catalog/d/catalog-bundle.tar", 9, s3) == b"x" * 9
    with pytest.raises(ValueError, match="BUNDLE_TOO_LARGE"):
        bundle.read_s3_object("s3://ai-catalog/catalog/d/catalog-bundle.tar", 8, s3)
