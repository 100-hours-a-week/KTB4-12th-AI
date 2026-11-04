"""Real migrations in a newly owned synthetic PG cluster only, never production."""
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from integration import migration_upgrade as upgrade
from integration.migrate_single_server import ROOT, ROLES

pytestmark=pytest.mark.skipif(not os.getenv('MIGRATION_SYNTHETIC_SOCKET'),reason='requires a newly owned synthetic PG cluster')

@pytest.fixture
def database(monkeypatch):
    socket=os.environ['MIGRATION_SYNTHETIC_SOCKET']
    assert socket.startswith('/tmp/ai-migration-')
    monkeypatch.setenv('INTEGRATION_SYNTHETIC_ONLY','1')
    monkeypatch.setenv('PROFILING_APP_DB_ROLE',ROLES['profiler'])
    monkeypatch.delenv('PROFILING_APP_DB_PASSWORD',raising=False)
    name='emmet4930_upgrade_'+uuid4().hex[:12]
    with psycopg.connect(dbname='postgres',user='fixture_owner',host=socket,autocommit=True) as db:
        db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    url=f'postgresql://fixture_owner@/{name}?host={socket}'
    yield name,url
    # Deliberately leave this synthetic DB in the disposable cluster until normal stop.


def upstream(url,target):
    engine=sa.create_engine(url.replace('postgresql://','postgresql+psycopg://'))
    try:
        with engine.begin() as c:
            cfg=Config(str(ROOT/'profiling/alembic.ini'));cfg.attributes['connection']=c
            command.upgrade(cfg,target)
    finally:engine.dispose()


def seed(db,revision):
    db.execute("INSERT INTO ai_profile.recipient_profiles(recipient_user_id,source_version,preferred_tags,disliked_categories) VALUES(42,7,%s,%s)",(json.dumps(['합성 차']) if revision<'0006' else json.dumps([{'axis':'카테고리','tag':'차','source':'preference','ref':'pref'}]),json.dumps([{'category_id':11,'category_name':'합성 비선호'}])))
    if revision<'0006':db.execute("UPDATE ai_profile.recipient_profiles SET disliked_tags='[\"합성 커피\"]'")
    if revision>='0002':db.execute("INSERT INTO ai_profile.profile_runs(recipient_user_id,source_version,input_hash,status,error) VALUES(42,7,'synthetic-hash','FAILED','{\"code\":\"synthetic\"}')")
    if revision>='0003':
        if revision<'0008':
            db.execute("INSERT INTO ai_catalog.categories(source_category_id,parent_source_category_id,name,level,product_count,backend_category_id,taxonomy_version) VALUES('group',NULL,'합성',1,1,1,'synthetic'),('leaf','group','합성 소분류',2,1,11,'synthetic')")
            db.execute("INSERT INTO ai_catalog.products(source_product_id,backend_product_id,name,brand,source_category_id,product_kind,product_type,description,unit_price,availability,view_count,source_provider,source_product_url,source_image_url,package_id) VALUES('SYNTHETIC:101',101,'합성 상품','합성','leaf','합성','Shipping','합성 검증',10000,'unknown',17,'SYNTHETIC','','','synthetic')")
        else:
            db.execute("INSERT INTO ai_catalog.categories(id,parent_id,name,level,product_count,taxonomy_version) VALUES(1,NULL,'합성',1,1,'synthetic'),(11,1,'합성 소분류',2,1,'synthetic')")
            db.execute("INSERT INTO ai_catalog.products(id,category_id,source_product_id,name,brand,product_kind,product_type,description,unit_price,availability,view_count,source_provider,source_product_url,source_image_url,package_id) VALUES(101,11,'SYNTHETIC:101','합성 상품','합성','합성','Shipping','합성 검증',10000,'unknown',17,'SYNTHETIC','','','synthetic')")
    if revision>='0005':db.execute("INSERT INTO ai_profile.recipient_reviews(recipient_user_id,product_id,rating,review_hash,position) VALUES(42,101,5,%s,0)",('b'*64,))


@pytest.mark.parametrize('revision',[None,*upgrade.KNOWN])
def test_known_versions_preserve_data_and_repeat(database,revision):
    name,url=database
    if revision:
        upstream(url,revision)
        with psycopg.connect(url) as db:seed(db,revision)
    receipt=upgrade.run_upgrade(url,name,upstream=True)
    assert receipt['alembicBefore']==revision and receipt['alembicAfter']=='0011' and receipt['recipientFunctionApplied'] and receipt['atomic']
    with psycopg.connect(url) as db:
        if revision:
            assert db.execute('SELECT source_version,disliked_categories FROM ai_profile.recipient_profiles WHERE recipient_user_id=42').fetchone()==(7,[{'category_id':11,'category_name':'합성 비선호'}])
            if revision<'0006':assert db.execute('SELECT preferred_tags,disliked_tags FROM ai_runtime.legacy_profile_tags WHERE recipient_user_id=42').fetchone()==(['합성 차'],['합성 커피'])
            if revision>='0002':assert db.execute('SELECT status,input_hash FROM ai_profile.profile_runs').fetchone()==('FAILED','synthetic-hash')
            if revision>='0003':assert db.execute('SELECT id,category_id,source_product_id,view_count FROM ai_catalog.products').fetchone()==(101,11,'SYNTHETIC:101',17)
            if '0003'<=revision<'0008':assert db.execute('SELECT source_category_id FROM ai_catalog.integration_category_sources WHERE category_id=11').fetchone()[0]=='leaf'
            if revision>='0005':assert db.execute('SELECT rating,review_hash FROM ai_profile.recipient_reviews').fetchone()==(5,'b'*64)
        cid=uuid4();tid=uuid4()
        db.execute('INSERT INTO ai_chat.conversations(id,sender,recipient) VALUES(%s,7,42)',(cid,))
        state={'turnId':str(tid),'status':'COMPLETED','message':'합성 질문','blocks':[{'type':'text','text':'합성 답변'}],'lastEventId':1}
        db.execute('INSERT INTO ai_chat.turns(id,conversation,state,context) VALUES(%s,%s,%s,%s)',(tid,cid,json.dumps(state),json.dumps({'synthetic':'kept'})))
        db.execute("INSERT INTO ai_chat.events(turn,seq,kind,payload) VALUES(%s,1,'turn.finished','{\"status\":\"COMPLETED\"}')",(tid,))
        db.execute("CREATE OR REPLACE FUNCTION ai_profile.chat_recipient_snapshot(conversation_id uuid) RETURNS jsonb LANGUAGE SQL AS $$ SELECT '{\"outdated\":true}'::jsonb $$")
    again=upgrade.run_upgrade(url,name,upstream=True)
    assert again['alembicBefore']=='0011' and again['recipientFunctionApplied']
    with psycopg.connect(url) as db:
        assert db.execute('SELECT state,context FROM ai_chat.turns WHERE id=%s',(tid,)).fetchone()==(state,{'synthetic':'kept'})
        assert db.execute('SELECT count(*) FROM ai_chat.events WHERE turn=%s',(tid,)).fetchone()[0]==1
        definition=db.execute("SELECT pg_get_functiondef('ai_profile.chat_recipient_snapshot(uuid)'::regprocedure)").fetchone()[0]
        assert 'guarded_input' in definition and 'outdated' not in definition
        assert db.execute("SELECT has_function_privilege('ai_chat_service','ai_profile.chat_recipient_snapshot(uuid)','EXECUTE')").fetchone()[0]
        if revision and revision<'0006':assert not db.execute("SELECT has_table_privilege('ai_chat_service','ai_runtime.legacy_profile_tags','SELECT')").fetchone()[0]


def test_final_step_failure_rolls_back_entire_old_upgrade(database,monkeypatch):
    name,url=database;upstream(url,'0005')
    with psycopg.connect(url) as db:seed(db,'0005')
    real=upgrade.migrate_connection
    def fail(*a):
        real(*a)
        raise RuntimeError('synthetic integration failure after DDL')
    monkeypatch.setattr(upgrade,'migrate_connection',fail)
    with pytest.raises(RuntimeError):upgrade.run_upgrade(url,name,upstream=True)
    with psycopg.connect(url) as db:
        assert db.execute('SELECT version_num FROM public.alembic_version').fetchone()[0]=='0005'
        assert db.execute('SELECT preferred_tags,disliked_tags FROM ai_profile.recipient_profiles').fetchone()==(['합성 차'],['합성 커피'])
        assert db.execute("SELECT to_regclass('ai_runtime.legacy_profile_tags')").fetchone()[0] is None
        assert db.execute('SELECT source_category_id FROM ai_catalog.categories ORDER BY level DESC LIMIT 1').fetchone()[0]=='leaf'
    monkeypatch.setattr(upgrade,'migrate_connection',real)
    assert upgrade.run_upgrade(url,name,upstream=True)['recipientFunctionApplied']


def test_missing_backend_id_stops_without_changes(database):
    name,url=database;upstream(url,'0007')
    with psycopg.connect(url) as db:
        seed(db,'0007');db.execute('UPDATE ai_catalog.products SET backend_product_id=NULL')
    with pytest.raises(ValueError):upgrade.run_upgrade(url,name,upstream=True)
    with psycopg.connect(url) as db:
        assert db.execute('SELECT version_num FROM public.alembic_version').fetchone()[0]=='0007'
        assert db.execute('SELECT source_category_id,backend_product_id FROM ai_catalog.products').fetchone()==('leaf',None)


def test_old_product_media_foreign_key_is_preserved(database):
    name,url=database;upstream(url,'0007')
    with psycopg.connect(url) as db:
        seed(db,'0007')
        db.execute('CREATE TABLE ai_catalog.product_media(source_product_id text REFERENCES ai_catalog.products(source_product_id),media_path text)')
        db.execute("INSERT INTO ai_catalog.product_media VALUES('SYNTHETIC:101','synthetic.svg')")
    upgrade.run_upgrade(url,name,upstream=True)
    with psycopg.connect(url) as db:
        assert db.execute('SELECT * FROM ai_catalog.product_media').fetchone()==('SYNTHETIC:101','synthetic.svg')
        with pytest.raises(psycopg.errors.ForeignKeyViolation):db.execute("INSERT INTO ai_catalog.product_media VALUES('missing','x')")


@pytest.mark.parametrize('kind',['unversioned','unknown','multiple','partial'])
def test_ambiguous_states_are_not_guessed(database,kind):
    name,url=database
    with psycopg.connect(url) as db:
        if kind=='unversioned':db.execute('CREATE SCHEMA ai_profile;CREATE TABLE ai_profile.keep_me(id int);INSERT INTO ai_profile.keep_me VALUES(1)')
        else:
            db.execute('CREATE TABLE public.alembic_version(version_num text)')
            if kind=='unknown':db.execute("INSERT INTO public.alembic_version VALUES('9999')")
            if kind=='multiple':db.execute("INSERT INTO public.alembic_version VALUES('0010'),('0011')")
            if kind=='partial':db.execute("INSERT INTO public.alembic_version VALUES('0011');CREATE SCHEMA ai_catalog;CREATE TABLE ai_catalog.generations(id int)")
    with pytest.raises(upgrade.MigrationBlocked):upgrade.run_upgrade(url,name,upstream=True)


def test_concurrent_migrate_does_not_start(database):
    name,url=database
    with psycopg.connect(url) as owner:
        owner.execute('SELECT pg_advisory_xact_lock(%s)',(upgrade.LOCK,))
        with pytest.raises(upgrade.MigrationBlocked,match='Another migration'):upgrade.run_upgrade(url,name,upstream=True)
    assert upgrade.run_upgrade(url,name,upstream=True)['atomic']


def test_legacy_raw_input_stops_without_invented_guard_data(database):
    name,url=database;upgrade.run_upgrade(url,name,upstream=True)
    with psycopg.connect(url) as db:
        db.execute("INSERT INTO ai_profile.recipient_inputs(recipient_user_id,source_version,input,guarded_input,guard_version,input_hash) VALUES(42,7,'{\"giftPreference\":\"synthetic\"}','{}','synthetic','kept-hash')")
        db.execute('ALTER TABLE ai_profile.recipient_inputs DROP COLUMN guarded_input,DROP COLUMN guard_version')
    with pytest.raises(ValueError,match='explicit guarded-data'):upgrade.run_upgrade(url,name,upstream=True)
    with psycopg.connect(url) as db:
        assert db.execute('SELECT input,input_hash FROM ai_profile.recipient_inputs').fetchone()==({'giftPreference':'synthetic'},'kept-hash')
        assert not db.execute("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='ai_profile' AND table_name='recipient_inputs' AND column_name='guarded_input')").fetchone()[0]


def test_stale_chat_columns_are_added_on_existing_identity(database):
    name,url=database;upgrade.run_upgrade(url,name,upstream=True)
    with psycopg.connect(url) as db:db.execute('ALTER TABLE ai_chat.turns DROP COLUMN requested_model,DROP COLUMN recipient_context,DROP COLUMN prepared_context')
    upgrade.run_upgrade(url,name,upstream=True)
    with psycopg.connect(url) as db:assert db.execute("SELECT requested_model,recipient_context,prepared_context FROM ai_chat.turns LIMIT 0").fetchall()==[]


def test_percent_dsn_and_cli_failures_do_not_expose_secrets(database):
    name,url=database
    encoded=url.replace('fixture_owner@','fixture_owner:synthetic%25password%3A@')
    assert upgrade.run_upgrade(encoded,name,upstream=True)['atomic']
    env={k:v for k,v in os.environ.items() if not any(w in k for w in ('TOKEN','SECRET','PASSWORD','DATABASE_URL','API_KEY'))}
    marker='synthetic-private-dsn-marker'
    env.update(MIGRATION_DATABASE_URL='postgresql://ai_migrator:'+marker+'@wrong-target/'+name,INTEGRATION_SYNTHETIC_ONLY='1',PYTHONPATH=str(ROOT))
    run=subprocess.run([sys.executable,'-B','-m','integration.migrate_single_server','--database',name,'--emmet'],cwd=ROOT,env=env,capture_output=True,text=True)
    assert run.returncode!=0 and marker not in run.stdout+run.stderr and 'postgresql://' not in run.stdout+run.stderr


def test_archive_revokes_inherited_serving_read_access(database):
    name,url=database;upstream(url,'0005')
    with psycopg.connect(url) as db:
        seed(db,'0005')
        db.execute('CREATE SCHEMA ai_runtime;ALTER DEFAULT PRIVILEGES IN SCHEMA ai_runtime GRANT SELECT ON TABLES TO ai_chat_service,ai_profiler_service')
    upgrade.run_upgrade(url,name,upstream=True)
    with psycopg.connect(url) as db:
        for role in ROLES.values():assert not db.execute("SELECT has_table_privilege(%s,'ai_runtime.legacy_profile_tags','SELECT')",(role,)).fetchone()[0]


def test_unknown_product_dependency_stops_and_preserves_rows(database):
    name,url=database;upstream(url,'0007')
    with psycopg.connect(url) as db:
        seed(db,'0007')
        db.execute('CREATE TABLE ai_catalog.custom_media(source_id text REFERENCES ai_catalog.products(source_product_id))')
        db.execute("INSERT INTO ai_catalog.custom_media VALUES('SYNTHETIC:101')")
    with pytest.raises(upgrade.MigrationBlocked,match='unsupported dependencies'):upgrade.run_upgrade(url,name,upstream=True)
    with psycopg.connect(url) as db:
        assert db.execute('SELECT version_num FROM public.alembic_version').fetchone()[0]=='0007'
        assert db.execute('SELECT * FROM ai_catalog.custom_media').fetchone()[0]=='SYNTHETIC:101'


def test_latest_recipient_function_reads_only_trusted_bound_recipient(database):
    name,url=database;upgrade.run_upgrade(url,name,upstream=True)
    cid=uuid4()
    with psycopg.connect(url) as db:
        db.execute('INSERT INTO ai_chat.conversations(id,sender,recipient) VALUES(%s,7,42)',(cid,))
        raw={'giftPreference':'합성 차 취향','dislikedCategories':[{'categoryId':11,'categoryName':'합성 비선호'}]}
        db.execute("INSERT INTO ai_profile.recipient_inputs(recipient_user_id,source_version,input,guarded_input,guard_version,input_hash) VALUES(42,7,%s,%s,'synthetic','synthetic-hash')",(json.dumps(raw),json.dumps(raw)))
        db.execute('SET LOCAL ROLE ai_chat_service')
        value=db.execute('SELECT ai_profile.chat_recipient_snapshot(%s)',(cid,)).fetchone()[0]
        assert value['status']=='AVAILABLE' and value['data']['recipientUserId']==42 and value['data']['giftPreference']=='합성 차 취향'
        assert value['data']['dislikedCategories']==raw['dislikedCategories']
        assert db.execute('SELECT ai_profile.chat_recipient_snapshot(%s)',(uuid4(),)).fetchone()[0]['status']=='NOT_PROVIDED'
        with pytest.raises(psycopg.errors.InsufficientPrivilege):db.execute('SELECT input FROM ai_profile.recipient_inputs')


def test_supplied_connection_never_reads_local_dotenv_or_changes_login_roles(database,monkeypatch):
    from profiling import settings
    name,url=database
    def forbidden(*a,**kw):raise AssertionError('Local dotenv must not be read')
    monkeypatch.setattr(settings,'read_dotenv',forbidden)
    upgrade.run_upgrade(url,name,upstream=True)
    with psycopg.connect(url) as db:
        assert db.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname='ai_profiler_service'").fetchone()[0] is False


def test_missing_application_role_blocks_before_alembic(database,monkeypatch):
    name,url=database
    monkeypatch.setattr(upgrade,'ROLES',dict(ROLES,profiler='synthetic_not_provisioned'))
    with pytest.raises(upgrade.MigrationBlocked,match='Preprovisioned'):upgrade.run_upgrade(url,name,upstream=True)
    with psycopg.connect(url) as db:assert db.execute("SELECT to_regclass('public.alembic_version')").fetchone()[0] is None
