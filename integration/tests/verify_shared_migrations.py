"""Synthetic fresh/0007-upgrade and existing-0008 audit. Own cluster only; no TCP."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import psycopg
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from integration.catalog_transition import upgrade_0008
from profiling.settings import get_settings

ROOT=Path(__file__).resolve().parents[2]
PG=Path('/opt/homebrew/opt/postgresql@18/bin')


def migrate(url, revision, location):
    os.environ['PROFILING_DATABASE_URL']=url
    get_settings.cache_clear()
    cfg=Config(str(ROOT/'profiling/alembic.ini'))
    cfg.set_main_option('script_location',str(location))
    command.upgrade(cfg,revision)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New disposable directory inside this candidate verification root')
    args=parser.parse_args()
    for key in list(os.environ):
        if any(word in key for word in ('TOKEN','SECRET','PASSWORD','API_KEY','DATABASE_URL')) or key.startswith('PROFILING_'):
            os.environ.pop(key)
    OUTPUT=args.output.resolve()
    if not OUTPUT.is_relative_to((ROOT.parent/'verification').resolve()):
        raise ValueError('Output must stay inside the candidate verification root')
    if OUTPUT.exists():raise RuntimeError('Use a fresh candidate verification directory')
    OUTPUT.mkdir()
    cluster=OUTPUT/'cluster';sock=Path(tempfile.mkdtemp(prefix='shared-db-',dir='/tmp'))
    subprocess.run([str(PG/'initdb'),'-D',str(cluster),'-U','fixture_owner','--auth=trust','--no-locale','-E','UTF8'],check=True,stdout=subprocess.DEVNULL)
    subprocess.run([str(PG/'pg_ctl'),'-D',str(cluster),'-l',str(OUTPUT/'postgres.log'),'-o',f"-k {sock} -c listen_addresses=''",'-w','start'],check=True,stdout=subprocess.DEVNULL)
    results={}
    try:
        with psycopg.connect(dbname='postgres',user='fixture_owner',host=str(sock),autocommit=True) as c:
            for db in ('emmet4930_shared_fresh','emmet4930_shared_upgrade'):
                c.execute('CREATE DATABASE '+db)
        def url(db):return f'postgresql+psycopg://fixture_owner@/{db}?host={sock}'
        os.environ['PROFILING_APP_DB_ROLE']='synthetic_shared_app'
        migrate(url('emmet4930_shared_fresh'),'head',ROOT/'profiling/alembic')
        with sa.create_engine(url('emmet4930_shared_fresh')).connect() as c:
            revision=c.exec_driver_sql('select version_num from alembic_version').scalar()
            privileges=c.exec_driver_sql("select has_table_privilege('synthetic_shared_app','ai_profile.recipient_profiles','SELECT'),has_table_privilege('synthetic_shared_app','ai_catalog.products','UPDATE')").one()
            assert revision=='0010' and tuple(privileges)==(True,False)
            results['fresh']={'revision':revision,'customRoleReadProfile':True,'catalogWriteDenied':True}
        old=OUTPUT/'old-alembic';shutil.copytree(ROOT/'profiling/alembic',old)
        (old/'versions/0007_app_role.py').write_bytes(subprocess.check_output(['git','-C',str(ROOT),'show','4930af63510d069244516619ac36f72bb53a86f0:profiling/alembic/versions/0007_app_role.py']))
        target=url('emmet4930_shared_upgrade');migrate(target,'0007',old)
        engine=sa.create_engine(target)
        with engine.begin() as c:
            c.exec_driver_sql("INSERT INTO ai_catalog.categories(source_category_id,parent_source_category_id,name,level,product_count,backend_category_id,taxonomy_version) VALUES('group',NULL,'합성',1,1,1,'synthetic'),('leaf','group','합성 소분류',2,1,11,'synthetic')")
            c.exec_driver_sql("INSERT INTO ai_catalog.products(source_product_id,backend_product_id,name,brand,source_category_id,product_kind,product_type,description,unit_price,availability,view_count,source_provider,source_product_url,source_image_url,package_id) VALUES('SYNTHETIC:101',101,'합성 상품','합성','leaf','합성','Shipping','합성 검증',10000,'unknown',17,'SYNTHETIC','','','synthetic')")
            c.exec_driver_sql('CREATE TABLE ai_catalog.product_media(source_product_id text REFERENCES ai_catalog.products(source_product_id), media_path text)')
            c.exec_driver_sql("INSERT INTO ai_catalog.product_media VALUES('SYNTHETIC:101','synthetic.svg')")
        results['upgrade']=upgrade_0008(engine)
        with engine.connect() as c:
            row=c.exec_driver_sql('SELECT id,category_id,source_product_id,view_count FROM ai_catalog.products').one()
            media=c.exec_driver_sql('SELECT * FROM ai_catalog.product_media').one()
            assert tuple(row)==(101,11,'SYNTHETIC:101',17) and tuple(media)==('SYNTHETIC:101','synthetic.svg')
            results['upgrade']['identitiesMediaAndPopularityPreserved']=True
        os.environ['PROFILING_APP_DB_ROLE']='synthetic_unapplied_role'
        with engine.begin() as c:
            c.exec_driver_sql("INSERT INTO ai_profile.recipient_profiles(recipient_user_id,source_version,preferred_tags) VALUES(88101,7,'[]')")
        migrate(target,'head',ROOT/'profiling/alembic')
        with engine.connect() as c:
            exists=c.exec_driver_sql("SELECT EXISTS(SELECT FROM pg_roles WHERE rolname='synthetic_unapplied_role')").scalar()
            assert not exists
            results['existing0008']={'updated0007Replayed':False,'newRoleCreated':False,'rewindOrRepair':False}
            row=c.exec_driver_sql('SELECT source_version,preferred_tags,disliked_tags FROM ai_profile.recipient_profiles WHERE recipient_user_id=88101').one()
            assert tuple(row)==(7,[],[])
            assert c.exec_driver_sql('SELECT version_num FROM alembic_version').scalar()=='0010'
            assert c.exec_driver_sql("SELECT format_type(atttypid,atttypmod) FROM pg_attribute WHERE attrelid='ai_catalog.products'::regclass AND attname='embedding'").scalar()=='vector(768)'
            results['upgrade0010']={'from':'0008','to':'0010','profilePreserved':True,'dislikedTagsDefault':[], 'embeddingColumn':'vector(768)','storageSwitched':False}
        with engine.begin() as c:
            c.exec_driver_sql("INSERT INTO ai_catalog.catalog_versions(package_id,taxonomy_version,product_count,category_count,is_active) VALUES('synthetic-native','synthetic',1,2,true)")
        env={**os.environ,'PROFILING_DATABASE_URL':target,'PROFILING_APP_DB_ROLE':'ai_app','PROFILING_TAGGER':'none','PYTHONDONTWRITEBYTECODE':'1'}
        tests=[sys.executable,'-B','-m','pytest','-q','-p','no:cacheprovider', 'tests/integration/test_db_embedding.py',
               'tests/integration/test_db_recipient_profiles.py','tests/integration/test_db_stores.py']
        with (OUTPUT/'upstream-db-tests.log').open('w') as log:
            run=subprocess.run(tests,cwd=ROOT/'profiling',env=env,stdout=log,stderr=subprocess.STDOUT,timeout=120)
        results['upstreamDbTests']={'command':tests,'exitCode':run.returncode,'log':'upstream-db-tests.log'}
        assert run.returncode==0, 'See upstream-db-tests.log'
        engine.dispose()
        get_settings.cache_clear()
        results['tagSpecDefault']=get_settings().TAG_SPEC
        assert results['tagSpecDefault']=='tag3disliked'
        results.update(synthetic=True,existingDatabasesTouched=False,paidCalls=0,passed=True)
    finally:
        subprocess.run([str(PG/'pg_ctl'),'-D',str(cluster),'-m','fast','-w','stop'],check=True,stdout=subprocess.DEVNULL)
        shutil.rmtree(sock)
        results['temporaryClusterStopped']=True
        (OUTPUT/'result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(results,ensure_ascii=False))


if __name__=='__main__':main()
