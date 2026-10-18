"""Owned new PostgreSQL cluster only; catalog assets reused, no old DB or private profiles copied."""

import argparse
import os, sys, json, hashlib, subprocess, socket, shutil, importlib.util
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT.parent / "verification"
PG = Path("/opt/homebrew/opt/postgresql@18/bin")
OLD = Path("/Users/yh/Codex/2026-10-06/task-3/team-chat-prep-20261013/worktree")
ASSETS = Path(
    "/Users/yh/Code/KTB/final/workspaces/product-search-branch/product-search/embedding"
)
DATA = ASSETS.parent / "data"
PACKAGE = Path(
    "/Users/yh/Code/KTB/final/output/handoff/product-catalog-20260922-v1/product-catalog-20260922-v1"
)
AUDIT = Path(
    "/Users/yh/Codex/2026-10-06/task-3/three-service-20261013/media-inventory.private.json"
)
sys.path[:0] = [str(ROOT), str(ROOT / "product-search")]
from integration.catalog_transition import upgrade_0008
from catalog_db import projection, encode, validate_generation


def run(cmd, cwd=ROOT, variables=None):
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(
            w in k
            for w in [
                "TOKEN",
                "SECRET",
                "PASSWORD",
                "API_KEY",
                "DATABASE",
                "PROFILING_",
                "CHAT_",
                "SEARCH_",
            ]
        )
    }
    env.update(
        PYTHONDONTWRITEBYTECODE="1",
        HF_HUB_OFFLINE="1",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
    )
    result = subprocess.run(
        list(map(str, cmd)),
        cwd=cwd,
        env=env | (variables or {}),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-3500:])
    return result.stdout


def snapshot(url, modern=False):
    with psycopg.connect(
        url, row_factory=dict_row, options="-c default_transaction_read_only=on"
    ) as db:
        version = db.execute(
            "SELECT * FROM ai_catalog.catalog_versions WHERE is_active"
        ).fetchone()
        gen = db.execute(
            "SELECT * FROM ai_catalog.search_generations WHERE catalog_version=%s",
            (version["id"],),
        ).fetchone()
        catalog = projection(db, version)
        validate_generation(
            catalog, gen["manifest"], bytes(gen["vectors"]), version["id"]
        )
        media = db.execute(
            "SELECT * FROM ai_catalog.product_media ORDER BY source_product_id,kind,ordinal"
        ).fetchall()
        identities = db.execute(
            "SELECT id,category_id,source_product_id FROM ai_catalog.products ORDER BY id"
            if modern
            else "SELECT backend_product_id id,c.backend_category_id category_id,p.source_product_id FROM ai_catalog.products p JOIN ai_catalog.categories c ON c.source_category_id=p.source_category_id ORDER BY backend_product_id"
        ).fetchall()
        return {
            "catalog": catalog,
            "manifest": gen["manifest"],
            "catalogHash": hashlib.sha256(encode(catalog)).hexdigest(),
            "vectorsHash": hashlib.sha256(bytes(gen["vectors"])).hexdigest(),
            "mediaHash": hashlib.sha256(encode(media)).hexdigest(),
            "mediaRows": len(media),
            "identitiesHash": hashlib.sha256(encode(identities)).hexdigest(),
            "products": len(catalog["products"]),
            "categories": len(catalog["taxonomy"]["categories"]),
        }


def main():
    global BASE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=BASE)
    args = parser.parse_args()
    BASE = args.output_root.resolve()
    if not BASE.is_relative_to(ROOT.parent):
        raise ValueError(
            "QA output must remain inside the isolated integration workspace"
        )
    BASE.mkdir(exist_ok=False)
    BASE.chmod(0o700)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    run(
        [
            PG / "initdb",
            "-D",
            BASE / "pg",
            "-U",
            "fixture_owner",
            "--auth-local=trust",
            "--auth-host=trust",
            "--no-locale",
            "-E",
            "UTF8",
        ]
    )
    started = False
    result = {
        "baseRevision": "4930af63510d069244516619ac36f72bb53a86f0",
        "existingDBTouched": False,
        "paidCalls": 0,
        "privateStatesCopied": False,
        "tests": [],
    }
    try:
        run(
            [
                PG / "pg_ctl",
                "-D",
                BASE / "pg",
                "-l",
                BASE / "postgres.log",
                "-o",
                f"-h 127.0.0.1 -c unix_socket_directories= -p {port}",
                "-w",
                "start",
            ]
        )
        started = True
        baseurl = f"postgresql://fixture_owner@127.0.0.1:{port}/postgres"
        with psycopg.connect(baseurl, autocommit=True) as db:
            for name in ["emmet4930_upgrade", "emmet4930_fresh"]:
                db.execute("CREATE DATABASE " + name)
        summaries = {}
        for name, modern in [("emmet4930_upgrade", False), ("emmet4930_fresh", True)]:
            url = baseurl.rsplit("/", 1)[0] + "/" + name
            variables = {
                "PROFILING_DATABASE_URL": url.replace(
                    "postgresql://", "postgresql+psycopg://"
                ),
                "PROFILING_TAGGER": "none",
            }
            run(
                [
                    ROOT / "profiling/.venv/bin/python",
                    "-B",
                    "-m",
                    "alembic",
                    "upgrade",
                    "0007",
                ],
                ROOT / "profiling",
                variables,
            )
            if modern:
                engine = sa.create_engine(variables["PROFILING_DATABASE_URL"])
                result["freshTransition"] = upgrade_0008(engine)
                engine.dispose()
            with psycopg.connect(url) as db:
                db.execute(
                    ((ROOT if modern else OLD) / "integration/schema.sql").read_text()
                )
            runtime = BASE / name / "search-root"
            (runtime / "embedding").mkdir(parents=True)
            for path in (ROOT / "product-search/embedding").iterdir():
                if path.is_file():
                    shutil.copyfile(path, runtime / "embedding" / path.name)
            for folder in ["models", "node_modules"]:
                (runtime / "embedding" / folder).symlink_to(
                    ASSETS / folder, target_is_directory=True
                )
            importer = ROOT if modern else OLD
            imported = json.loads(
                run(
                    [
                        importer / "product-search/.venv/bin/python",
                        "-B",
                        importer / "integration/import_catalog.py",
                        "--runtime",
                        runtime,
                        "--data",
                        DATA,
                        "--package",
                        PACKAGE,
                        "--audit",
                        AUDIT,
                        "--media",
                        BASE / name / "media",
                    ],
                    variables={"CATALOG_OWNER_DATABASE_URL": url},
                )
            )
            assert (
                imported["vectorsReusedAndHashVerified"]
                and imported["essentialFieldMismatches"] == 0
            )
            before = snapshot(url, modern)
            if not modern:
                engine = sa.create_engine(variables["PROFILING_DATABASE_URL"])
                spec = importlib.util.spec_from_file_location(
                    "unmodified0008",
                    ROOT / "profiling/alembic/versions/0008_catalog_backend_ids.py",
                )
                migration = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(migration)
                try:
                    with engine.begin() as db:
                        with Operations.context(MigrationContext.configure(db)):
                            migration.upgrade()
                except sa.exc.DBAPIError as error:
                    assert error.orig.sqlstate == "2BP01", error.orig.sqlstate
                    result["originalMigrationReproduction"] = (
                        "2BP01 product_media dependency; transaction rolled back"
                    )
                else:
                    raise AssertionError("Expected media dependency failure")
                assert snapshot(url) == before
                result["upgradeTransition"] = upgrade_0008(engine)
                engine.dispose()
                after = snapshot(url, True)
                assert before == after
                result["tests"].append(
                    "upgrade preserves exact canonical JSON/manifest/vector order/media/product-category IDs"
                )
            else:
                after = before
                result["tests"].append(
                    "fresh install0001..0007 plus unchanged0008 wrapper and full import"
                )
            with psycopg.connect(
                url, options="-c default_transaction_read_only=on"
            ) as db:
                assert (
                    db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                    == "0008"
                )
                assert (
                    db.execute(
                        "SELECT count(*) FROM ai_catalog.product_media m LEFT JOIN ai_catalog.products p ON p.source_product_id=m.source_product_id WHERE p.id IS NULL"
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    db.execute(
                        "SELECT count(*) FROM ai_profile.profile_runs"
                    ).fetchone()[0]
                    == 0
                )
            summaries[name] = {
                k: after[k]
                for k in [
                    "products",
                    "categories",
                    "mediaRows",
                    "catalogHash",
                    "vectorsHash",
                    "mediaHash",
                    "identitiesHash",
                ]
            }
            summaries[name]["catalogVersion"] = after["catalog"]["ai_catalog_version"]
            facts = dict(after["catalog"])
            facts.pop("ai_catalog_version")
            summaries[name]["versionIndependentFactsHash"] = hashlib.sha256(
                encode(facts)
            ).hexdigest()
        assert (
            summaries["emmet4930_upgrade"]["versionIndependentFactsHash"]
            == summaries["emmet4930_fresh"]["versionIndependentFactsHash"]
        )
        assert (
            summaries["emmet4930_upgrade"]["vectorsHash"]
            == summaries["emmet4930_fresh"]["vectorsHash"]
        )
        result["databases"] = summaries
        result["tests"].append(
            "fresh and upgraded catalogs have identical4231-product/67-category facts and vectors"
        )
        (BASE / "cluster.json").write_text(
            json.dumps(
                {"port": port, "qaOnly": True, "baseUrl": baseurl, "root": str(BASE)}
            )
        )
        result["passed"] = True
        (BASE / "transition-result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    finally:
        if started:
            run([PG / "pg_ctl", "-D", BASE / "pg", "-m", "smart", "-w", "stop"])


if __name__ == "__main__":
    main()
