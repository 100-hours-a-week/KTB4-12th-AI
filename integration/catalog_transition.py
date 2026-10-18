"""Explicit owner transaction around unchanged Emmet0008; no cascade or data deletion."""

import argparse
import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT = Path(__file__).resolve().parents[1]


def upgrade_0008(engine):
    with engine.begin() as db:
        if (
            not db.execute(sa.text("SELECT current_database()"))
            .scalar_one()
            .startswith("emmet4930_")
        ):
            raise ValueError(
                "This local transition executable is restricted to separate emmet4930_ QA databases"
            )
        db.execute(sa.text("SET LOCAL lock_timeout='5s'"))
        revision = db.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        if revision == "0008":
            if (
                db.execute(
                    sa.text(
                        "SELECT to_regclass('ai_catalog.integration_category_sources')"
                    )
                ).scalar_one()
                is None
            ):
                raise ValueError(
                    "0008 has no preserved source-category provenance; verified mapping import required"
                )
            return {"revision": revision, "alreadyApplied": True}
        if revision != "0007":
            raise ValueError(
                "Transition requires exactly0007; run upstream Alembic through0007 first"
            )
        db.execute(
            sa.text(
                "LOCK TABLE ai_catalog.products,ai_catalog.categories IN ACCESS EXCLUSIVE MODE"
            )
        )
        missing = db.execute(
            sa.text(
                "SELECT (SELECT count(*) FROM ai_catalog.products WHERE backend_product_id IS NULL)+(SELECT count(*) FROM ai_catalog.categories WHERE backend_category_id IS NULL)"
            )
        ).scalar_one()
        if missing:
            raise ValueError(
                "Backend IDs missing; transition refused before any schema change"
            )
        dependencies = db.execute(
            sa.text("""SELECT conrelid::regclass::text table_name, conname
            FROM pg_constraint WHERE contype='f' AND confrelid='ai_catalog.products'::regclass""")
        ).all()
        allowed = [("ai_catalog.product_media", "product_media_source_product_id_fkey")]
        if any(tuple(row) not in allowed for row in dependencies):
            raise ValueError(
                "Unknown product foreign-key dependency; explicit preservation design required"
            )
        db.execute(
            sa.text("""CREATE TABLE IF NOT EXISTS ai_catalog.integration_category_sources(
            category_id bigint PRIMARY KEY, source_category_id text UNIQUE NOT NULL)""")
        )
        db.execute(
            sa.text("""INSERT INTO ai_catalog.integration_category_sources
            SELECT backend_category_id,source_category_id FROM ai_catalog.categories""")
        )
        db.execute(
            sa.text(
                "ALTER TABLE ai_catalog.products ADD CONSTRAINT integration_products_source_id_key UNIQUE(source_product_id)"
            )
        )
        for table, name in dependencies:
            db.execute(
                sa.text("LOCK TABLE ai_catalog.product_media IN ACCESS EXCLUSIVE MODE")
            )
            db.execute(
                sa.text(
                    "ALTER TABLE ai_catalog.product_media DROP CONSTRAINT product_media_source_product_id_fkey"
                )
            )
        path = ROOT / "profiling/alembic/versions/0008_catalog_backend_ids.py"
        spec = importlib.util.spec_from_file_location("emmet_0008_unmodified", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
        db.execute(
            sa.text("""ALTER TABLE ai_catalog.integration_category_sources ADD CONSTRAINT
            integration_category_sources_category_fkey FOREIGN KEY(category_id) REFERENCES ai_catalog.categories(id)""")
        )
        for table, name in dependencies:
            db.execute(
                sa.text("""ALTER TABLE ai_catalog.product_media ADD CONSTRAINT product_media_source_product_id_fkey
                FOREIGN KEY(source_product_id) REFERENCES ai_catalog.products(source_product_id)""")
            )
        # Existing category readers need the same source-ID information after it moves to the sidecar.
        # Copy only pre-existing SELECT readers; create no new role or authentication authority.
        readers = (
            db.execute(
                sa.text(
                    "SELECT DISTINCT grantee FROM information_schema.role_table_grants WHERE table_schema='ai_catalog' AND table_name='categories' AND privilege_type='SELECT'"
                )
            )
            .scalars()
            .all()
        )
        for role in readers:
            grantee = (
                "PUBLIC"
                if role == "PUBLIC"
                else db.dialect.identifier_preparer.quote_identifier(role)
            )
            db.execute(
                sa.text(
                    "GRANT SELECT ON ai_catalog.integration_category_sources TO "
                    + grantee
                )
            )
        changed = db.execute(
            sa.text(
                "UPDATE alembic_version SET version_num='0008' WHERE version_num='0007'"
            )
        )
        if changed.rowcount != 1:
            raise RuntimeError("Migration version update lost its expected predecessor")
        return {
            "revision": "0008",
            "mediaForeignKeysPreserved": len(dependencies),
            "sourceCategoryMappingsPreserved": db.execute(
                sa.text("SELECT count(*) FROM ai_catalog.integration_category_sources")
            ).scalar_one(),
        }


if __name__ == "__main__":
    import os, json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    engine = sa.create_engine(
        os.environ["CATALOG_OWNER_DATABASE_URL"].replace(
            "postgresql://", "postgresql+psycopg://"
        )
    )
    try:
        print(json.dumps(upgrade_0008(engine)))
    finally:
        engine.dispose()
