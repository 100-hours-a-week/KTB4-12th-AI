"""Empty, private smoke DB only. Public synthetic fixture and runtime-only fake secrets."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql
from uuid import uuid4

from integration.migrate_single_server import migrate, ROLES

from integration.containers.entrypoint import prepare_encoder


def stage_catalog(runtime, url, fixture_path):
    """Publish the same three-product fixture for native and container launchers."""
    from search.complete_export import digest
    from tools.sync_postgres import sync
    fixture = json.loads(Path(fixture_path).read_text())
    if fixture.get('synthetic') is not True:
        raise ValueError('Only the explicit synthetic catalog fixture is allowed')
    categories = {'message':'synthetic','data':{'categories':[{'categoryId':1,'name':'합성 선물','children':[
        {'categoryId':11,'name':'컵'}, {'categoryId':12,'name':'음향'}]}]}}
    names = {'CAT-CUP':(11,'컵'), 'CAT-AUDIO':(12,'음향')}
    export = {'message':'synthetic','data':{'generatedAt':'2026-10-17T00:00:00Z','products':[
        {'productId':p['id'],'name':p['name'],'brand':'SYNTHETIC','description':'합성 시험 상품',
         'categoryId':names[p['category']][0],'categoryName':names[p['category']][1],
         'price':p['price'],'available':p['availability']!='unavailable','viewCount':p['views'],
         'updatedAt':'2026-10-17T00:00:00Z'} for p in fixture['products']]}}
    export['data']['snapshot']={'id':str(uuid4()),'complete':True,'productCount':3,
                               'sha256':digest(export['data']['products'],categories)}
    return sync(runtime, url, lambda:(export,categories), apply=True)


def main():
    if os.environ.get("INTEGRATION_SYNTHETIC_ONLY") != "1":
        raise ValueError("Synthetic-only initialization required")
    database = os.environ["AI_DATABASE_NAME"]
    from integration.database_identity import validate_database_name
    validate_database_name(database)
    url = "postgresql://fixture_owner@postgres:5432/" + database
    with psycopg.connect(url) as db:
        if db.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema IN ('ai_catalog','ai_profile','ai_chat')"
        ).fetchone()[0]:
            raise ValueError("Never overwrite/reinitialize existing smoke data")
    with psycopg.connect(url) as db:
        for role in [*ROLES.values(), 'ai_console_service']:
            # This cluster is new, private and disposable. Existing roles are an error.
            db.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE').format(sql.Identifier(role)))
    env = os.environ | {
        "PROFILING_DATABASE_URL": url.replace("postgresql://", "postgresql+psycopg://"),
        "PROFILING_TAGGER": "none", "PROFILING_APP_DB_ROLE": ROLES['profiler'],
    }
    subprocess.run(["python", "-m", "alembic", "upgrade", "0011"], cwd="/workspace/profiling", env=env, check=True)
    migrate(url, database)
    with psycopg.connect(url) as db:
        db.execute('CREATE TABLE ai_catalog.integration_marker(id boolean PRIMARY KEY CHECK(id))')
        db.execute('INSERT INTO ai_catalog.integration_marker VALUES(true)')
        db.execute('GRANT USAGE ON SCHEMA ai_runtime,ai_catalog,ai_chat,ai_profile TO ai_console_service')
        db.execute('GRANT SELECT ON ai_runtime.database_identity,ai_catalog.integration_marker,ai_chat.turns,ai_chat.events,ai_profile.profile_runs,ai_catalog.generation_products,ai_catalog.catalog_state TO ai_console_service')
    shared = Path("/shared")
    shared.mkdir(exist_ok=True)
    runtime = shared / "catalog"
    prepare_encoder(runtime, "/assets")
    stage_catalog(runtime, url.replace('fixture_owner@','ai_catalog_sync@'),
                  '/workspace/integration/tests/fixture.json')
    # PG now holds a pending generation. Only the serving Search activates it.
    (shared / "media").mkdir(exist_ok=True)
    for p in Path("/workspace/integration/tests/media").glob("*.svg"):
        shutil.copyfile(p, shared / "media" / p.name)
    from console.admin_auth import password_hash
    password = "synthetic-admin-" + uuid4().hex
    for name, value in [("console_admin_password", password), ("console_admin.hash", password_hash(password))]:
        (shared / name).write_text(value)
        (shared / name).chmod(0o600)
    fake = "synthetic-container-fixture-only"
    for name in [
        "ai_service_token",
        "openrouter_key",
        "session_secret",
        "qa_admin_password",
        "demo_access_token",
    ]:
        (shared / name).write_text(fake)
        (shared / name).chmod(0o600)
    for name, role in [
        ("profiler_db", "ai_profiler_service"),
        ("chat_db", "ai_chat_service"),
        ("search_db", "ai_search_service"),
        ("console_db", "ai_console_service"),
    ]:
        value = url.replace("fixture_owner@", role + "@")
        if name == "profiler_db":
            value = value.replace("postgresql://", "postgresql+psycopg://")
        if name == "chat_db":
            value += "?options=-csearch_path%3Dai_chat,public"
        (shared / name).write_text(value)
        (shared / name).chmod(0o600)
    (shared / "unready_db").write_text(
        "postgresql+psycopg://ai_profiler_service@postgres:5432/emmet4930_absent"
    )
    (shared / "provider-mode.json").write_text(json.dumps({"mode": "success"}))
    (shared / "backend-state.json").write_text(
        json.dumps(
            {
                "synthetic": True,
                "recipientUserId": 42,
                "sourceVersion": 1,
                "giftPreference": "합성 컵 선물",
                "dislikedCategories": [],
                "callback": None,
            }
        )
    )
    (shared / "backend-state.json").chmod(0o600)
    print(
        "Synthetic DB/catalog and runtime-only dummy values prepared; no real key/ledger/provider used."
    )


if __name__ == "__main__":
    main()
