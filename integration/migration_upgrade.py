"""Existing migrate job, one transaction: preserve old fields, upgrade, refresh Chat.

Historical Alembic revisions are not rewritten. Unknown/unversioned populated
schemas and source data we cannot interpret stop before commit.
"""
import os
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy.pool import NullPool
from alembic import command
from alembic.config import Config

from integration.migrate_single_server import ROOT, ROLES, migrate_connection

TARGET = '0011'
LOCK = 367109731501
KNOWN = [f'{i:04}' for i in range(1,12)]


class MigrationBlocked(ValueError):
    """Only fixed operator-safe messages, never SQL, DSNs or row values."""


def current_revision(db):
    exists = db.execute("SELECT to_regclass('public.alembic_version')").fetchone()[0]
    rows = db.execute('SELECT version_num FROM public.alembic_version').fetchall() if exists else []
    if len(rows)>1 or rows and rows[0][0] not in KNOWN:
        raise MigrationBlocked('Unknown or multiple Alembic revisions; no changes committed')
    if not rows:
        found = db.execute("SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_schema IN ('ai_profile','ai_catalog','ai_chat','ai_search'))").fetchone()[0]
        if found:raise MigrationBlocked('Populated or unversioned AI schema; explicit version reconciliation required')
    return rows[0][0] if rows else None


def preserve_old_tags(connection):
    # Keep exact old tag values, without inventing new axes/source labels.
    # The serving roles receive no SELECT grant to this migration-only archive.
    connection.exec_driver_sql('''CREATE SCHEMA IF NOT EXISTS ai_runtime;
        CREATE TABLE IF NOT EXISTS ai_runtime.legacy_profile_tags(
            recipient_user_id bigint PRIMARY KEY, source_version bigint NOT NULL,
            preferred_tags jsonb NOT NULL, disliked_tags jsonb NOT NULL)''')
    connection.exec_driver_sql('''INSERT INTO ai_runtime.legacy_profile_tags
        SELECT recipient_user_id,source_version,preferred_tags,disliked_tags
        FROM ai_profile.recipient_profiles''')
    for role in ['PUBLIC', *ROLES.values()]:
        connection.exec_driver_sql('REVOKE ALL ON ai_runtime.legacy_profile_tags FROM '+role)
    for role in ROLES.values():
        if connection.execute(sa.text("SELECT has_table_privilege(:role,'ai_runtime.legacy_profile_tags','SELECT')"),{'role':role}).scalar_one():
            raise MigrationBlocked('Legacy archive inherits application access; no changes committed')


@contextmanager
def role_environment():
    # Never import a local .env password or mutate database login credentials.
    keys={'PROFILING_APP_DB_ROLE':ROLES['profiler'],'PROFILING_APP_DB_PASSWORD':''}
    old={key:os.environ.get(key) for key in keys}
    os.environ.update(keys)
    try:yield
    finally:
        for key,value in old.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value


def upgrade_alembic(connection, revision):
    cfg=Config(str(ROOT/'profiling/alembic.ini'))
    cfg.attributes['connection']=connection
    def step(target):command.upgrade(cfg,target)
    with role_environment():
        # Pause only at revisions with destructive historical transformations.
        if revision is None or revision<'0006':
            step('0005')
            preserve_old_tags(connection)
        if revision is None or revision<'0008':
            step('0007')
            from integration.catalog_transition import upgrade_0008_on_connection
            try:upgrade_0008_on_connection(connection)
            except ValueError:
                raise MigrationBlocked('Catalog key migration blocked: missing Backend IDs or unsupported dependencies; no changes committed') from None
        step(TARGET)


def run_upgrade(url, database, *, upstream=False):
    from integration.database_identity import validate_database_name, validate_dsn
    validate_database_name(database)
    synthetic=os.getenv('INTEGRATION_SYNTHETIC_ONLY')=='1' and database.startswith('emmet4930_')
    if not synthetic:validate_dsn(url,database,'ai_migrator')
    if os.getenv('PROFILING_APP_DB_PASSWORD'):
        raise MigrationBlocked('Role credential changes are not part of migration')
    engine=sa.create_engine(url.replace('postgresql://','postgresql+psycopg://'),poolclass=NullPool,hide_parameters=True)
    try:
        with engine.begin() as connection:
            db=connection.connection.driver_connection
            assert db is not None
            name,role=db.execute('SELECT current_database(),current_user').fetchone()
            if name!=database or role not in (('ai_migrator','fixture_owner') if synthetic else ('ai_migrator',)):
                raise MigrationBlocked('Wrong migration database or role')
            if not db.execute('SELECT pg_try_advisory_xact_lock(%s)',(LOCK,)).fetchone()[0]:
                raise MigrationBlocked('Another migration is running; no changes committed')
            db.execute("SET LOCAL lock_timeout='5s'")
            db.execute("SET LOCAL search_path=public")
            for role in ROLES.values():
                row=db.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=%s',(role,)).fetchone()
                if row is None or any(row):raise MigrationBlocked('Preprovisioned nonprivileged application roles required')
            before=current_revision(db)
            identity=db.execute("SELECT to_regclass('ai_runtime.database_identity')").fetchone()[0]
            if identity:
                row=db.execute('SELECT database_name,version FROM ai_runtime.database_identity WHERE singleton').fetchone()
                if row is not None and row!=(database,'single-server/1'):
                    raise MigrationBlocked('Unknown integration version; no changes committed')
            elif db.execute("SELECT to_regclass('ai_catalog.generations')").fetchone()[0]:
                raise MigrationBlocked('Partial unversioned catalog installation; no changes committed')
            if upstream:upgrade_alembic(connection,before)
            elif before!=TARGET:raise MigrationBlocked('Run this migrate job with --emmet to upgrade the known Alembic history')
            from integration.recipient_data import RecipientInputMigrationRequired
            try:result=migrate_connection(db,database)
            except RecipientInputMigrationRequired:
                raise MigrationBlocked('Existing recipient inputs require explicit guarded-data migration; no changes committed') from None
            # migrate_connection always runs the checked-in CREATE OR REPLACE SQL.
            return {**result,'alembicBefore':before,'alembicAfter':TARGET,
                    'recipientFunctionApplied':True,'atomic':True}
    finally:engine.dispose()
