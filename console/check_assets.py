"""Read-only readiness check; never downloads assets or invokes a model."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedding-assets", type=Path, required=True)
    parser.add_argument("--pg-bin", type=Path, required=True)
    args = parser.parse_args()
    errors = []
    lock = json.loads((ROOT / "product-search/embedding/model.lock.json").read_text())
    for record in lock["files"]:
        path = args.embedding_assets / "models" / record["path"]
        if not path.is_file() or path.stat().st_size != record["bytes"]:
            errors.append("Missing/wrong-size model: " + record["path"])
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != record["sha256"]:
            errors.append("Model hash mismatch: " + record["path"])
    dependencies = json.loads(
        (ROOT / "product-search/embedding/package.json").read_text()
    )["dependencies"]
    for package, version in dependencies.items():
        path = args.embedding_assets / "node_modules" / package / "package.json"
        if not path.is_file() or json.loads(path.read_text()).get("version") != version:
            errors.append("Missing/wrong-version Node dependency: " + package)
    for name in (
        "runtime.mjs",
        "documents.mjs",
        "service.mjs",
        "model.lock.json",
        "package-lock.json",
    ):
        if not (args.embedding_assets / name).is_file():
            errors.append("Missing encoder file: " + name)
    node = shutil.which("node")
    node_version = None
    if node:
        node_version = subprocess.check_output(
            [node, "--version"], text=True, timeout=5
        ).strip()
    else:
        errors.append("Node missing from PATH")
    pg_config = args.pg_bin / "pg_config"
    pg_version = None
    vector_version = None
    if pg_config.is_file():
        pg_version = subprocess.check_output(
            [str(pg_config), "--version"], text=True, timeout=5
        ).strip()
        share = Path(
            subprocess.check_output(
                [str(pg_config), "--sharedir"], text=True, timeout=5
            ).strip()
        )
        library = Path(
            subprocess.check_output(
                [str(pg_config), "--pkglibdir"], text=True, timeout=5
            ).strip()
        )
        control = share / "extension/vector.control"
        if control.is_file():
            vector_version = next(
                (
                    line.split("=", 1)[1].strip().strip("'")
                    for line in control.read_text().splitlines()
                    if line.startswith("default_version")
                ),
                None,
            )
        if not control.is_file() or not any(
            (library / name).is_file() for name in ("vector.so", "vector.dylib")
        ):
            errors.append("Matching PostgreSQL pgvector control/library missing")
    else:
        errors.append("pg_config missing under --pg-bin")
    for name in ("initdb", "pg_ctl"):
        if not (args.pg_bin / name).is_file():
            errors.append("PostgreSQL tool missing: " + name)
    print(
        json.dumps(
            {
                "ready": not errors,
                "readOnly": True,
                "model": lock["model"],
                "revision": lock["revision"],
                "modelBytes": sum(i["bytes"] for i in lock["files"]),
                "nodeVersion": node_version,
                "postgresVersion": pg_version,
                "pgvectorVersion": vector_version,
                "errors": errors,
            },
            indent=2,
        )
    )
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
