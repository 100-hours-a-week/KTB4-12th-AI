"""Isolated PG scenarios; only synthetic data. Run through run_single_server_tests.py."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import numpy as np
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest
import sqlalchemy as sa

from catalog_runtime import CatalogRuntime
from catalog_store import CatalogStore, Generation, materialize_generation
from integration.migrate_single_server import migrate, ROLES
from search.complete_export import digest
from search.reloading import ReloadingSearch
from search.service import SearchService
from search.visibility import denied_products
from tools.sync_postgres import sync

ROOT = Path(__file__).resolve().parents[2]
FINGERPRINT = 'b' * 64
pytestmark = pytest.mark.skipif(not os.getenv('SINGLE_SERVER_TEST_CLUSTER_URL'), reason='Run with isolated PG scenario runner')


@pytest.fixture(params=['fresh', 'upgrade'])
def database(monkeypatch, request):
    base = os.environ['SINGLE_SERVER_TEST_CLUSTER_URL']
    assert base.startswith('postgresql://fixture_owner@127.0.0.1:')
    name = 'emmet4930_ss_' + uuid4().hex[:12]
    with psycopg.connect(base, autocommit=True) as db:
        db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    owner = base.rsplit('/', 1)[0] + '/' + name
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PROFILING_', 'OPENROUTER_', 'DATABASE_', 'SEARCH_', 'CHAT_', 'STORE_'))}
    env.update(PROFILING_DATABASE_URL=owner.replace('postgresql://', 'postgresql+psycopg://'),
               PROFILING_TAGGER='none', PROFILING_APP_DB_ROLE=ROLES['profiler'],
               PYTHONPATH=str(ROOT/'profiling/src'), PYTHONDONTWRITEBYTECODE='1')
    target='0010' if request.param=='upgrade' else '0011'
    result = subprocess.run([sys.executable, '-B', '-m', 'alembic', 'upgrade', target], cwd=ROOT/'profiling', env=env, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    if request.param=='upgrade':
        with psycopg.connect(owner) as db:db.execute('CREATE SCHEMA ai_chat AUTHORIZATION ai_chat_service')
        with psycopg.connect(owner.replace('fixture_owner@','ai_chat_service@'), options='-csearch_path=ai_chat,public') as db:
            db.execute((ROOT/'chat/sql/state.sql').read_text())
    # A real pre-0011 row must survive the upgrade, with personal disliked tags intact.
    with psycopg.connect(owner) as db:
        db.execute("INSERT INTO ai_profile.recipient_profiles(recipient_user_id,source_version,preferred_tags,disliked_tags) VALUES(42,7,%s,%s)",
                   (Jsonb([{'axis':'카테고리','tag':'꽃','source':'preference','ref':'pref'}]), Jsonb([{'axis':'브랜드','tag':'소니','source':'preference','ref':'pref'}])))
    result = subprocess.run([sys.executable, '-B', '-m', 'alembic', 'upgrade', '0011'], cwd=ROOT/'profiling', env=env, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    monkeypatch.setenv('INTEGRATION_SYNTHETIC_ONLY', '1')
    assert migrate(owner, name)['changed']
    assert not migrate(owner, name)['changed']
    urls = {key: owner.replace('fixture_owner@', role+'@') for key, role in ROLES.items()}
    return {'owner': owner, 'name': name, 'migrationMode':request.param, **urls}


@pytest.fixture
def source(tmp_path):
    root = tmp_path/'runtime'
    (root/'embedding').mkdir(parents=True)
    shutil.copy2(ROOT/'product-search/embedding/model.lock.json', root/'embedding/model.lock.json')
    categories = {'message':'synthetic','data':{'categories':[{'categoryId':1,'name':'합성','children':[{'categoryId':11,'name':'컵'}]}]}}
    export = {'message':'synthetic','data':{'generatedAt':'2026-10-16T01:00:00Z','products':[
        {'productId':i,'name':f'합성 컵 {i}','brand':'SYNTHETIC','description':'합성 데이터',
         'categoryId':11,'categoryName':'컵','price':i*10,'available':True,'viewCount':i,
         'updatedAt':'2026-10-16T00:00:00Z'} for i in (101,102,103)]}}
    calls=[]
    def seal():
        export['data']['snapshot']={'id':str(uuid4()),'complete':True,'productCount':len(export['data']['products']),
                                  'sha256':digest(export['data']['products'],categories)}
    seal()
    def embed(root, texts, directory):
        calls.append(list(texts))
        vectors=np.zeros((len(texts),768),dtype=np.float32)
        for i,text in enumerate(texts):vectors[i,int(hashlib.sha256(text.encode()).hexdigest()[:6],16)%768]=1
        return vectors,[10]*len(texts)
    return root,export,categories,seal,embed,calls


def prepare(database, source):
    root,export,categories,seal,embed,calls=source
    return sync(root,database['sync'],lambda:(copy.deepcopy(export),copy.deepcopy(categories)),apply=True,
                embed=embed,fingerprint=lambda *_:FINGERPRINT)


async def serving(database, source, monkeypatch):
    monkeypatch.setattr('search.reloading.encoder_contract',lambda *_:FINGERPRINT)
    store=CatalogStore(database['search']);state=store.state()
    generation=store.load(state['active_id'] or state['pending_id'])
    directory=materialize_generation(generation,source[0])
    owner=ReloadingSearch(source[0],SearchService(directory),None,encoder_fingerprint=FINGERPRINT)
    runtime=CatalogRuntime(store,source[0],owner)
    await runtime.poll()
    assert await runtime.check()
    return owner,runtime


async def close(owner,runtime):
    await owner.close()
    if runtime.connection is not None:runtime.connection.close()


@pytest.mark.asyncio
async def test_generation_activation_persistence_cache_and_tombstones(database,source,monkeypatch):
    receipt=prepare(database,source)
    assert CatalogStore(database['search']).state()['active_id'] is None
    owner,runtime=await serving(database,source,monkeypatch)
    root,export,categories,seal,embed,calls=source
    first=owner.current
    try:
        assert prepare(database,source)['state']=='ALREADY_ACTIVE'
        before=first.get_products([101])['products'][0]['price']
        export['data']['generatedAt']='2026-10-16T02:00:00Z'
        export['data']['products'][0]['price']=99
        seal();second=prepare(database,source)
        assert second['reuseVectors']==3 and second['embedDocuments']==0
        assert first.get_products([101])['products'][0]['price']==before
        binding,pinned=await owner.bindings.acquire(first.snapshot_id)
        await runtime.poll()
        assert owner.current.get_products([101])['products'][0]['price']==99
        assert pinned.get_products([101])['products'][0]['price']==before
        assert owner.current.catalog['products'][0]['product_tags']
        # Confirmed deletion remains denied even while an old generation is retained.
        export['data']['generatedAt']='2026-10-16T03:00:00Z'
        export['data']['products'].pop(0)
        seal();prepare(database,source)
        token=denied_products.set(runtime.store.denied())
        try:
            assert pinned.get_products([101])['missingIds']==[101]
            assert all(h['productId']!=101 for h in (await pinned.search({'query':'','mode':'lexical'}))['hits'])
        finally:denied_products.reset(token)
        await runtime.poll()
        await owner.bindings.release(binding)
        active=runtime.store.state()['active_id']
        projection=owner.current.catalog
        shutil.rmtree(root/'data')
        restored=materialize_generation(runtime.store.load(active),root)
        rebuilt=SearchService(restored)
        try:assert rebuilt.catalog==projection
        finally:rebuilt.close()
        # Rollback cannot undo deletion knowledge.
        writer=CatalogStore(database['sync'])
        with writer.writer():writer.propose(receipt['catalogVersion'])
        await runtime.poll()
        token=denied_products.set(runtime.store.denied())
        try:assert owner.current.get_products([101])['missingIds']==[101]
        finally:denied_products.reset(token)
    finally:await close(owner,runtime)


@pytest.mark.asyncio
async def test_complete_empty_snapshot_and_rollback_keep_all_deletions(database,source,monkeypatch):
    first=prepare(database,source);owner,runtime=await serving(database,source,monkeypatch)
    root,export,categories,seal,embed,calls=source
    try:
        export['data']['generatedAt']='2026-10-16T02:00:00Z'
        export['data']['products']=[];seal()
        empty=prepare(database,source)
        await runtime.poll()
        assert await runtime.check() and empty['products']==0
        assert (await owner.current.search({'query':'컵','mode':'lexical'}))['hits']==[]
        assert runtime.store.denied()==frozenset({101,102,103})
        writer=CatalogStore(database['sync'])
        with writer.writer():writer.propose(first['catalogVersion'])
        await runtime.poll()
        token=denied_products.set(runtime.store.denied())
        try:
            assert (await owner.current.search({'query':'','mode':'lexical'}))['hits']==[]
            assert owner.current.get_products([101,102,103])['missingIds']==[101,102,103]
        finally:denied_products.reset(token)
    finally:await close(owner,runtime)


@pytest.mark.asyncio
async def test_failure_keeps_active_and_incomplete_snapshot_does_not_delete(database,source,monkeypatch):
    initial=prepare(database,source);owner,runtime=await serving(database,source,monkeypatch)
    root,export,categories,seal,embed,calls=source
    try:
        export['data']['products'].pop()
        export['data']['generatedAt']='2026-10-16T02:00:00Z'
        with pytest.raises(ValueError,match='COUNT'):prepare(database,source)
        assert not runtime.store.denied()
        seal();export['data']['products'][0]['name']='새 합성 컵';seal()
        def broken(*_):raise RuntimeError('synthetic encoder failure')
        with pytest.raises(RuntimeError,match='synthetic encoder'):
            sync(root,database['sync'],lambda:(copy.deepcopy(export),copy.deepcopy(categories)),apply=True,embed=broken,fingerprint=lambda *_:FINGERPRINT)
        assert runtime.store.state()['active_id']==initial['catalogVersion']
        assert runtime.store.denied()==frozenset({103})
        prepare(database,source);await runtime.poll()
        assert await runtime.check()
        good=runtime.store.load(runtime.store.state()['active_id'])
        bad_manifest=dict(good.manifest,encoder_fingerprint='c'*64)
        bad_id=str(uuid4());catalog=good.catalog;catalog['ai_catalog_version']=bad_id
        from search.export_catalog import encode
        raw=encode(catalog);bad_manifest['catalog_sha256']=hashlib.sha256(raw).hexdigest()
        bad=Generation(bad_id,good.export_id,raw,bad_manifest,good.vectors)
        writer=CatalogStore(database['sync'])
        with writer.writer():writer.prepare(bad);writer.propose(bad_id)
        await runtime.poll()
        assert runtime.store.state()['active_id']==good.id
        assert runtime.store.state()['last_error']=='GENERATION_VALIDATION_FAILED'
    finally:await close(owner,runtime)


def test_sql_ownership_and_0011_upgrade(database):
    with psycopg.connect(database['owner']) as db:
        row=db.execute('SELECT preferred_tags,disliked_tags FROM ai_profile.recipient_profiles WHERE recipient_user_id=42').fetchone()
        assert row[0][0]['tag']=='꽃' and row[1][0]['tag']=='소니'
    forbidden={'chat':['SELECT * FROM ai_catalog.generations','SELECT * FROM ai_profile.recipient_profiles','CREATE TABLE ai_chat.bad(id int)'],
               'profiler':['SELECT * FROM ai_catalog.generations','SELECT * FROM ai_chat.turns','DELETE FROM ai_profile.read_grants'],
               'search':['SELECT * FROM ai_profile.recipient_profiles','DELETE FROM ai_catalog.generations'],
               'sync':['UPDATE ai_catalog.catalog_state SET active_id=NULL','SELECT * FROM ai_chat.turns']}
    for role, statements in forbidden.items():
        for statement in statements:
            with psycopg.connect(database[role]) as db:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute(statement)
    from integration.database_identity import validate_dsn
    with pytest.raises(ValueError):validate_dsn('postgresql://ai_chat_service@postgres/emmet4930_other',database['name'],'ai_chat_service')


def test_profile_read_requires_grant_and_exact_version(database,monkeypatch):
    from profiling import profile_read
    grant=str(uuid4());expires=datetime.now(timezone.utc)+timedelta(minutes=15)
    with psycopg.connect(database['owner']) as db:
        db.execute('INSERT INTO ai_profile.read_grants(id,caller_user_id,recipient_user_id,source_version,expires_at) VALUES(%s,11,42,7,%s)',(grant,expires))
    engine=sa.create_engine(database['profiler'].replace('postgresql://','postgresql+psycopg://'))
    app=FastAPI();app.state.engine=engine;app.include_router(profile_read.router)
    monkeypatch.setattr(profile_read,'get_settings',lambda:type('Settings',(),{'SERVICE_TOKEN':'synthetic-profile-token'})())
    request={'grantId':grant,'callerUserId':11,'recipientUserId':42,'sourceVersion':7}
    try:
        with TestClient(app) as client:
            assert client.post('/internal/v1/profiles/read',json=request).status_code==401
            headers={'Authorization':'Bearer synthetic-profile-token'}
            assert client.post('/internal/v1/profiles/read',json=request|{'callerUserId':12},headers=headers).status_code==403
            result=client.post('/internal/v1/profiles/read',json=request,headers=headers)
            assert result.status_code==200 and result.json()['dislikedTags'][0]['tag']=='소니'
            assert result.json()['tagTaxonomy'] is None  # No invented current hash for a legacy row.
            assert result.json()['catalogVersion'] is None
            assert result.headers['cache-control']=='no-store'
            assert 'giftPreference' not in result.json() and 'reviews' not in result.json()
            with psycopg.connect(database['owner']) as db:db.execute('UPDATE ai_profile.recipient_profiles SET source_version=8 WHERE recipient_user_id=42')
            assert client.post('/internal/v1/profiles/read',json=request,headers=headers).status_code==409
            with psycopg.connect(database['owner']) as db:db.execute('UPDATE ai_profile.read_grants SET revoked_at=now() WHERE id=%s',(grant,))
            assert client.post('/internal/v1/profiles/read',json=request,headers=headers).status_code==403
            assert client.post('/internal/v1/profiles/read',json=request,headers=[(b'authorization','Bearer 한글'.encode())]).status_code==401
    finally:engine.dispose()




@pytest.mark.asyncio
async def test_pg_restart_and_uncertain_activation_reconcile(database,source,monkeypatch):
    prepare(database,source);owner,runtime=await serving(database,source,monkeypatch)
    root,export,categories,seal,embed,calls=source
    try:
        # A second Search process cannot take over while the first holds the lease.
        other=CatalogRuntime(CatalogStore(database['search']),root,owner)
        with pytest.raises(RuntimeError,match='ONLY_ONE_SEARCH'):other.claim()
        ctl=os.environ['SINGLE_SERVER_TEST_PG_CTL'];cluster=os.environ['SINGLE_SERVER_TEST_PG_DATA']
        subprocess.run([ctl,'-D',cluster,'-l',str(Path(cluster).parent/'postgres.log'),'-m','fast','-w','restart'],capture_output=True,check=True,timeout=30)
        assert not await runtime.check()
        await runtime.poll();assert await runtime.check()
        export['data']['generatedAt']='2026-10-16T02:00:00Z';export['data']['products'][0]['price']=199
        seal();pending=prepare(database,source)
        # Simulate a crash after PG commits but before the in-memory pointer swaps.
        runtime.store.activate(pending['catalogVersion'],runtime.store.state()['active_id'],runtime.connection)
        assert not await runtime.check()
        await runtime.poll();assert await runtime.check()
        assert owner.current.get_products([101])['products'][0]['price']==199
        # Writer connection loss cannot publish through another connection.
        writer=CatalogStore(database['sync'])
        with writer.writer():
            pid=writer._writer_connection.info.backend_pid
            with psycopg.connect(database['owner']) as db:db.execute('SELECT pg_terminate_backend(%s)',(pid,))
            with pytest.raises(psycopg.Error):writer.propose(pending['catalogVersion'])
        assert await runtime.check()
    finally:await close(owner,runtime)


@pytest.mark.skipif(not os.getenv('SINGLE_SERVER_TEST_ENCODER_ASSETS'),reason='Explicit existing encoder assets required')
def test_real_cpu_http_and_service_restart(database,source):
    from integration.tests.single_server_http import run
    assert run(database,source)['passed']




def test_profiler_durable_transition_guard_includes_failed_and_unsent_runs(database):
    from integration.profiler_rebind import unfinished_profile_runs
    from profiling.stores import DbProfileRunStore
    from profiling.types import ProfileOutcome, RunStatus, SearchResult
    engine=sa.create_engine(database['profiler'].replace('postgresql://','postgresql+psycopg://'))
    try:
        store=DbProfileRunStore(engine)
        outcome=ProfileOutcome(recipient_user_id=81,source_version=1,status=RunStatus.RUNNING,input_hash='0'*64)
        for status in [RunStatus.RUNNING,RunStatus.FAILED,RunStatus.RESULT_READY,RunStatus.DELIVERED]:
            value=outcome.model_copy(update={'status':status})
            if status in (RunStatus.RESULT_READY,RunStatus.DELIVERED):
                value.search=SearchResult(product_ids=[],query_text='',catalog_version_id=uuid4())
            store.save(value)
            assert unfinished_profile_runs(engine)==(0 if status is RunStatus.DELIVERED else 1)
    finally:engine.dispose()


@pytest.mark.skipif(not os.getenv('SINGLE_SERVER_TEST_ENVIRONMENTS'),reason='Explicit Chat environment required')
def test_chat_closes_admission_before_draining_owned_turn(database):
    code='''import os
from uuid import uuid4
from app.store import Store
from app.errors import APIError
s=Store(os.environ['SYNTHETIC_STORE_DSN'])
owner=str(uuid4());cid=str(uuid4());other=str(uuid4());tid=str(uuid4())
try:
 assert s.lease(owner)
 s.conversation(cid,7,42);s.conversation(other,7,42)
 assert s.start(cid,tid,'합성 진행 중 질문',owner)[1]
 assert s.pause_admissions(True)=={'paused':True}
 try:s.start(other,str(uuid4()),'새 합성 질문',owner)
 except APIError as error:assert error.status==503
 else:raise AssertionError('New admission passed closed gate')
 assert s.start(cid,tid,'합성 진행 중 질문',owner)[1] is False
 assert s.emit(cid,tid,'turn.finished',{'status':'COMPLETED'},owner)
 assert s.read(cid)['turns'][0]['status']=='COMPLETED'
 assert s.pause_admissions(False)=={'paused':False}
 assert s.start(other,str(uuid4()),'재개한 합성 질문',owner)[1]
finally:s.close()
'''
    python=Path(os.environ['SINGLE_SERVER_TEST_ENVIRONMENTS'])/'chat/.venv/bin/python'
    env={k:v for k,v in os.environ.items() if not any(t in k for t in ('TOKEN','PASSWORD','SECRET','API_KEY','DATABASE_URL'))}
    env.update(PYTHONPATH=str(ROOT/'chat'),PYTHONDONTWRITEBYTECODE='1',
               SYNTHETIC_STORE_DSN=database['chat']+'?options=-csearch_path%3Dai_chat,public')
    completed=subprocess.run([str(python),'-B','-c',code],env=env,capture_output=True,text=True,timeout=20)
    assert completed.returncode==0,completed.stderr


def test_standalone_chat_schema_wrapper_is_explicit_and_repeatable(database):
    base = os.environ['SINGLE_SERVER_TEST_CLUSTER_URL']
    name = 'emmet4930_chat_' + uuid4().hex[:12]
    with psycopg.connect(base, autocommit=True) as db:
        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname='ai_migrator'").fetchone():
            db.execute('CREATE ROLE ai_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE')
        db.execute(sql.SQL('CREATE DATABASE {} OWNER ai_migrator').format(sql.Identifier(name)))
    url = base.rsplit('/', 1)[0].replace('fixture_owner@', 'ai_migrator@') + '/' + name
    psql = str(Path(os.environ['SINGLE_SERVER_TEST_PG_CTL']).with_name('psql'))
    command = [psql, url, '-X', '-v', 'ON_ERROR_STOP=1', '-v', 'expected_database='+name,
               '-f', str(ROOT/'chat/sql/migrate.sql')]
    for _ in range(2):
        done = subprocess.run(command, capture_output=True, text=True, timeout=15)
        assert done.returncode == 0, done.stderr
    with psycopg.connect(url.replace('ai_migrator@', 'ai_chat_service@')) as db:
        assert db.execute('SELECT version FROM ai_chat.schema_version').fetchone()[0] == 'single-server/1'
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute('CREATE TABLE ai_chat.no_app_ddl(id int)')
    with psycopg.connect(url) as db:
        db.execute("INSERT INTO ai_chat.runtime_lease VALUES(1,'synthetic-running-owner',now()+interval '1 minute')")
    denied = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert denied.returncode != 0 and 'Stop this candidate Chat' in denied.stderr


@pytest.mark.asyncio
async def test_backup_restore_preserves_data_grants_and_unknown_cost(database, source, tmp_path, monkeypatch):
    """Real pg_dump/restore into a new DB; never rewrites the source identity."""
    prepare(database, source)
    owner, runtime = await serving(database, source, monkeypatch)
    await close(owner, runtime)
    source[1]['data']['generatedAt'] = '2026-10-16T02:00:00Z'
    source[1]['data']['products'].pop()
    source[3]()
    prepare(database, source)
    from profiling.stores import DbProfileRunStore
    from profiling.types import ProfileOutcome, RunStatus
    engine = sa.create_engine(database['profiler'].replace('postgresql://', 'postgresql+psycopg://'))
    try:
        DbProfileRunStore(engine).save(ProfileOutcome(recipient_user_id=81, source_version=1,
            status=RunStatus.RUNNING, input_hash='0' * 64))
    finally:
        engine.dispose()
    def snapshot(url):
        with psycopg.connect(url) as db:
            tables = db.execute("SELECT schemaname,tablename FROM pg_tables WHERE schemaname IN ('ai_runtime','ai_catalog','ai_search','ai_chat','ai_profile') OR (schemaname='public' AND tablename='alembic_version') ORDER BY 1,2").fetchall()
            data = {}
            for schema, name in tables:
                rows = db.execute(sql.SQL("SELECT to_jsonb(t)::text FROM {}.{} t ORDER BY to_jsonb(t)::text").format(sql.Identifier(schema), sql.Identifier(name))).fetchall()
                data[schema + '.' + name] = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
            data['grants'] = db.execute("SELECT table_schema,table_name,grantee,privilege_type FROM information_schema.role_table_grants WHERE table_schema LIKE 'ai_%' ORDER BY 1,2,3,4").fetchall()
            return data

    before = snapshot(database['owner'])
    pg = Path(os.environ['SINGLE_SERVER_TEST_PG_CTL']).parent
    backup = tmp_path / 'synthetic.dump'
    # This URL has only an ephemeral loopback fixture role and no password.
    dump = subprocess.run([str(pg/'pg_dump'), '--format=custom', '--no-password',
                           '--dbname', database['owner'], '--file', str(backup)],
                          capture_output=True, text=True, timeout=20)
    assert dump.returncode == 0, dump.stderr
    listing = subprocess.run([str(pg/'pg_restore'), '--list', str(backup)],
                             capture_output=True, text=True, timeout=10)
    base = os.environ['SINGLE_SERVER_TEST_CLUSTER_URL']
    name = 'emmet4930_restore_' + uuid4().hex[:12]
    with psycopg.connect(base, autocommit=True) as db:
        db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    restored = base.rsplit('/', 1)[0] + '/' + name
    restore = subprocess.run([str(pg/'pg_restore'), '--no-password', '--exit-on-error',
                              '--dbname', restored, str(backup)],
                             capture_output=True, text=True, timeout=20)
    assert restore.returncode == 0, restore.stderr
    assert snapshot(restored) == before
    assert snapshot(database['owner']) == before
    with psycopg.connect(restored) as db:
        assert db.execute('SELECT product_id FROM ai_catalog.product_tombstones').fetchall() == [(103,)]
    # A differently named restore must not silently become a serving database.
    with pytest.raises(ValueError, match='Unexpected existing integration migration'):
        migrate(restored, name)
    role_url = restored.replace('fixture_owner@', 'ai_chat_service@')
    with psycopg.connect(role_url) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute('SELECT * FROM ai_profile.recipient_profiles')
    proof = {'migrationMode': database['migrationMode'], 'dumpSha256': hashlib.sha256(backup.read_bytes()).hexdigest(),
             'tablesCompared': len(before) - 1, 'sourceUnchanged': True,
             'unknownReservationCharged': True, 'roleGrantsPreserved': True,
             'differentDatabaseIdentityBlocked': True}
    output = Path(os.environ['SINGLE_SERVER_TEST_OUTPUT'])
    (output / ('backup-restore-' + database['migrationMode'] + '.json')).write_text(json.dumps(proof, indent=2) + '\n')
