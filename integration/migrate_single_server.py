"""Explicit migration for a prepared isolated DB; applications never execute DDL.

Run Emmet Alembic through 0011 first. Roles must already exist; this command
never creates credentials or makes an existing role LOGIN/superuser.
"""
import argparse
import os
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
VERSION = "single-server/1"
ROLES = {"chat": "ai_chat_service", "profiler": "ai_profiler_service",
         "search": "ai_search_service", "sync": "ai_catalog_sync"}


def migrate(url, expected_database):
    if not expected_database.startswith("emmet4930_"):
        raise ValueError("Explicit dedicated integration database required")
    with psycopg.connect(url) as db:
        name, owner = db.execute("SELECT current_database(),current_user").fetchone()
        if name != expected_database or (owner != "ai_migrator" and not (owner == "fixture_owner" and os.getenv("INTEGRATION_SYNTHETIC_ONLY") == "1")):
            raise ValueError("Wrong migration database/role")
        db.execute("SELECT pg_advisory_xact_lock(367109731501)")
        if db.execute("SELECT version_num FROM public.alembic_version").fetchone()[0] != "0011":
            raise ValueError("Emmet migration0011 must be applied first")
        for role in ROLES.values():
            row = db.execute("SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
            if row is None or any(row):
                raise ValueError("Preprovisioned nonprivileged application roles required")
        db.execute("CREATE SCHEMA IF NOT EXISTS ai_runtime")
        db.execute("CREATE TABLE IF NOT EXISTS ai_runtime.database_identity(singleton boolean PRIMARY KEY CHECK(singleton), database_name text NOT NULL, version text NOT NULL)")
        old = db.execute("SELECT database_name,version FROM ai_runtime.database_identity WHERE singleton").fetchone()
        if old:
            if old != (expected_database, VERSION):
                raise ValueError("Unexpected existing integration migration")
            return {"database": name, "version": VERSION, "changed": False}
        db.execute((ROOT / "product-search/sql/catalog.sql").read_text())
        db.execute("CREATE SCHEMA IF NOT EXISTS ai_search")
        db.execute("CREATE TABLE IF NOT EXISTS ai_search.searches(id text PRIMARY KEY,created_at text,request text,response text)")
        db.execute("CREATE TABLE IF NOT EXISTS ai_search.feedback(id text PRIMARY KEY,search_id text REFERENCES ai_search.searches(id) ON DELETE CASCADE,created_at text,product_id bigint,verdict text,note text)")
        db.execute("CREATE SCHEMA IF NOT EXISTS ai_chat")
        # A prior candidate let Chat own its schema. Transfer only objects in the
        # explicitly named candidate DB; do not alter cluster roles or logins.
        for schema in ("ai_chat", "ai_search"):
            db.execute(sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(sql.Identifier(schema), sql.Identifier(owner)))
            for table, in db.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s AND tableowner=ANY(%s)",
                                    (schema, list(ROLES.values()))).fetchall():
                db.execute(sql.SQL("ALTER TABLE {}.{} OWNER TO {}").format(sql.Identifier(schema), sql.Identifier(table), sql.Identifier(owner)))
            for name_, arguments in db.execute("""SELECT p.proname,pg_get_function_identity_arguments(p.oid)
                    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner
                    WHERE n.nspname=%s AND r.rolname=ANY(%s)""", (schema,list(ROLES.values()))).fetchall():
                db.execute(sql.SQL("ALTER FUNCTION {}.{}({}) OWNER TO {}").format(sql.Identifier(schema),
                    sql.Identifier(name_),sql.SQL(arguments),sql.Identifier(owner)))
        db.execute("SET LOCAL search_path=ai_chat,public")
        db.execute((ROOT / "chat/sql/state.sql").read_text())
        db.execute("""CREATE TABLE ai_profile.read_grants(id uuid PRIMARY KEY,caller_user_id bigint NOT NULL,
            recipient_user_id bigint NOT NULL,source_version bigint NOT NULL CHECK(source_version>0),
            expires_at timestamptz NOT NULL,revoked_at timestamptz)""")
        # Existing legacy SELECT grants are removed only in this explicitly named
        # new candidate DB. Nothing connects to or repairs the current live DB.
        for domain, role in ROLES.items():
            r = sql.Identifier(role)
            db.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(name), r))
            db.execute(sql.SQL("GRANT USAGE ON SCHEMA ai_runtime TO {}").format(r))
            db.execute(sql.SQL("GRANT SELECT ON ai_runtime.database_identity TO {}").format(r))
            db.execute(sql.SQL("GRANT SELECT ON public.alembic_version TO {}").format(r))
            if domain in ("chat", "profiler"):
                db.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA ai_catalog REVOKE ALL ON TABLES FROM {}").format(r))
                db.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA ai_catalog FROM {}").format(r))
                db.execute(sql.SQL("REVOKE ALL ON SCHEMA ai_catalog FROM {}").format(r))
        for schema, domain in (("ai_chat", "chat"), ("ai_profile", "profiler"), ("ai_search", "search")):
            r, s = sql.Identifier(ROLES[domain]), sql.Identifier(schema)
            db.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(s, r))
            db.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(s, r))
            db.execute(sql.SQL("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(s, r))
        # Profiler can use a grant but cannot mint authority for a caller.
        db.execute("REVOKE INSERT,UPDATE,DELETE ON ai_profile.read_grants FROM ai_profiler_service")
        db.execute("REVOKE INSERT,UPDATE,DELETE ON ai_chat.schema_version FROM ai_chat_service")
        db.execute("GRANT USAGE ON SCHEMA ai_catalog TO ai_search_service,ai_catalog_sync")
        db.execute("GRANT SELECT ON ALL TABLES IN SCHEMA ai_catalog TO ai_search_service,ai_catalog_sync")
        db.execute("GRANT INSERT ON ai_catalog.generations,ai_catalog.export_receipts,ai_catalog.product_tombstones TO ai_catalog_sync")
        db.execute("GRANT UPDATE(pending_id,last_error) ON ai_catalog.catalog_state TO ai_catalog_sync")
        db.execute("GRANT UPDATE(active_id,pending_id,revision,last_error,activated_at) ON ai_catalog.catalog_state TO ai_search_service")
        db.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        db.execute("INSERT INTO ai_runtime.database_identity VALUES(true,%s,%s)", (name, VERSION))
        return {"database": name, "version": VERSION, "changed": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--emmet", action="store_true", help="Apply upstream Alembic first in this explicitly named candidate DB")
    args = parser.parse_args()
    import json
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        url = Path(os.environ["MIGRATION_DATABASE_URL_FILE"]).read_text().strip()
    if args.emmet:
        from integration.database_identity import validate_dsn
        import subprocess
        import sys
        validate_dsn(url, args.database, "ai_migrator")
        if os.getenv("PROFILING_APP_DB_PASSWORD"):
            raise ValueError("Role credential mutation is not part of migration")
        env = os.environ | {"PROFILING_DATABASE_URL": url.replace("postgresql://", "postgresql+psycopg://"),
                            "PROFILING_APP_DB_ROLE": ROLES["profiler"], "PROFILING_TAGGER": "none"}
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0011"], cwd=ROOT/"profiling", env=env, check=True)
    print(json.dumps(migrate(url, args.database)))
