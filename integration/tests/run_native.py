"""Actual isolated DB + three service processes, synthetic data, zero paid calls.

No existing DB/ports/configuration are accepted or altered. Temporary processes
and DB are always stopped. Existing public encoder assets may be read only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def until(check, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            result = check()
            if result:
                return result
        except (httpx.HTTPError, KeyError, ValueError):
            pass
        time.sleep(.1)
    raise AssertionError('Synthetic integration wait timed out')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pg-bin', type=Path, required=True)
    parser.add_argument('--embedding-assets', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--real-catalog', type=Path)
    parser.add_argument('--source-package', type=Path)
    parser.add_argument('--media-audit', type=Path)
    parser.add_argument('--media-root', type=Path)
    parser.add_argument('--browser', type=Path)
    args = parser.parse_args()
    python = {service: str(ROOT / service / '.venv/bin/python') for service in ['chat', 'product-search', 'profiling']}
    for p in python.values():
        if not Path(p).exists():
            raise ValueError('Run each service uv sync first')
    scratch = Path(tempfile.mkdtemp(prefix='ktb-ai-integration-'))
    scratch.chmod(0o700)
    env = {k: v for k, v in os.environ.items() if not any(x in k for x in ['TOKEN', 'SECRET', 'PASSWORD', 'API_KEY', 'DATABASE', 'PROFILING', 'SEARCH_', 'CHAT_', 'STORE_', 'OPENROUTER'])}
    env.update(PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1', OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
    children, logs = [], []
    pg_started = False
    result = {'synthetic': True, 'paidModelCalls': 0, 'existingDBOrServicesChanged': False,
              'hostArchitecture': platform.machine(), 'postgresVersion': None, 'tests': []}
    def run(cmd, *, cwd=ROOT, variables=None):
        completed = subprocess.run(cmd, cwd=cwd, env=env | (variables or {}), capture_output=True, text=True, timeout=240)
        if completed.returncode:
            raise RuntimeError('Integration subprocess failed: ' + (completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else str(completed.returncode)))
        return completed
    def start(name, cmd, cwd, variables):
        log = (scratch / (name + '.log')).open('w'); logs.append(log)
        process = subprocess.Popen(cmd, cwd=cwd, env=env | variables, stdout=log, stderr=subprocess.STDOUT)
        children.append(process)
        return process
    def passed(name):
        result['tests'].append(name)
    try:
        port = free_port()
        data = scratch / 'pg'
        run([str(args.pg_bin/'initdb'), '-D', str(data), '-U', 'fixture_owner', '--auth-local=trust', '--auth-host=trust', '--no-locale', '-E', 'UTF8'])
        run([str(args.pg_bin/'pg_ctl'), '-D', str(data), '-l', str(scratch/'postgres.log'), '-o', f'-h 127.0.0.1 -k {scratch} -p {port}', '-w', 'start'])
        pg_started = True
        bootstrap = f'postgresql://fixture_owner@127.0.0.1:{port}/postgres'
        with psycopg.connect(bootstrap,autocommit=True) as db:db.execute('CREATE DATABASE emmet4930_native')
        url = bootstrap.rsplit('/',1)[0]+'/emmet4930_native'
        owner_env = {'PROFILING_DATABASE_URL': url.replace('postgresql://', 'postgresql+psycopg://'), 'PROFILING_TAGGER': 'none'}
        run([python['profiling'], '-m', 'alembic', 'upgrade', '0007'], cwd=ROOT/'profiling', variables=owner_env)
        run([python['profiling'],'-B','-m','integration.catalog_transition'],variables={'PYTHONPATH':str(ROOT),'CATALOG_OWNER_DATABASE_URL':url})
        with psycopg.connect(url) as db:
            db.execute((ROOT/'integration/schema.sql').read_text())
            result['postgresVersion'] = db.execute('SHOW server_version').fetchone()[0]
        passed('profiler_migrations0001_to0008_and_integration_schema')
        media = scratch / 'media'
        shutil.copytree(ROOT/'integration/tests/media', media)
        runtime = scratch / 'search-root'
        (runtime/'embedding').mkdir(parents=True)
        for path in (ROOT/'product-search/embedding').iterdir():
            if path.is_file():
                shutil.copyfile(path, runtime/'embedding'/path.name)
        for name in ['models', 'node_modules']:
            asset = args.embedding_assets/name
            if not asset.is_dir():
                raise ValueError('Existing public embedding assets missing: '+name)
            (runtime/'embedding'/name).symlink_to(asset.resolve(), target_is_directory=True)
        lock = json.loads((runtime/'embedding/model.lock.json').read_text())
        for file in lock['files']:
            assert hashlib.sha256((runtime/'embedding/models'/file['path']).read_bytes()).hexdigest() == file['sha256']
        if args.real_catalog:
            if not all([args.source_package,args.media_audit,args.media_root]):
                raise ValueError('Full catalog test requires package, verified audit and persistent media root')
            media=args.media_root
            pub=run([python['product-search'],str(ROOT/'integration/import_catalog.py'),'--runtime',str(runtime),
                     '--data',str(args.real_catalog),'--package',str(args.source_package),'--audit',str(args.media_audit),'--media',str(media)],
                    variables={'CATALOG_OWNER_DATABASE_URL':url})
            result['realCatalogImport']=imported=json.loads(pub.stdout)
            binding=imported['binding']
            full=json.loads((args.real_catalog/'catalog.json').read_text())['products']
            expected_ids={p['id'] for p in full}; first_id=full[0]['id']
            result.update(realProductData=True,synthetic=False,syntheticChatAndProfileRequests=True)
        else:
            pub = run([python['product-search'], str(ROOT/'integration/catalog.py'), str(runtime)], variables={'CATALOG_OWNER_DATABASE_URL': url})
            binding = json.loads(pub.stdout)
            expected_ids={101,102};first_id=101
        result['catalogBinding'] = binding
        passed('single_canonical_product_and_real_encoder_vector_generation')
        search_port, profile_port, chat_port, callback_port = [free_port() for _ in range(4)]
        search_url, profile_url, chat_url, callback_url = [f'http://127.0.0.1:{p}' for p in [search_port, profile_port, chat_port, callback_port]]
        # Synthetic credentials exist only in this test process, never in defaults.
        service_token = 'synthetic-integration-' + uuid.uuid4().hex
        def service_cmd(service, module, p):
            return [python[service], '-B', '-m', 'uvicorn', module, '--host', '127.0.0.1', '--port', str(p), '--workers', '1', '--no-access-log']
        start('callback', service_cmd('profiling', 'callback_app:app', callback_port), ROOT/'integration/tests', {})
        search_db = url.replace('fixture_owner@', 'ai_search_service@')
        start('search', service_cmd('product-search', 'app:app', search_port), ROOT/'product-search', {
            'SEARCH_CATALOG_DATABASE_URL': search_db, 'SEARCH_QA_DATABASE_URL': search_db, 'SEARCH_RUNTIME_ROOT': str(runtime), 'SEARCH_MEDIA_ROOT': str(media)})
        profile_db = url.replace('fixture_owner@', 'ai_app@')
        profile_variables = {
            'PYTHONPATH':os.pathsep.join([str(ROOT),str(ROOT/'profiling/src')]),
            'PROFILING_DATABASE_URL': profile_db.replace('postgresql://', 'postgresql+psycopg://'),
            'PROFILING_CATALOG_SOURCE': 'db', 'PROFILING_DB_REFUSE_SUPERUSER': 'true',
            'PROFILING_SERVICE_TOKEN': service_token, 'PROFILING_BACKEND_BASE_URL': callback_url,
            'PROFILING_TAGGER': 'none', 'PROFILING_EMBEDDING_DIR': str(runtime), 'PROFILING_EMBEDDING_REQUIRED': 'true',
            'PROFILING_CALLBACK_MAX_ATTEMPTS': '1', 'PROFILING_CATALOG_POLL_TTL_S': '0'}
        profile_process = start('profiling', service_cmd('profiling', 'integration.profiler_free_runtime:app', profile_port), ROOT/'profiling', profile_variables)
        chat_db = url.replace('fixture_owner@', 'ai_chat_service@') + '?options=-csearch_path%3Dai_chat,public'
        start('chat', service_cmd('chat', 'app.main:app', chat_port), ROOT/'chat', {
            'AI_SERVICE_TOKEN': service_token, 'SESSION_SECRET': 'synthetic-session-' + uuid.uuid4().hex,
            'QA_ADMIN_PASSWORD': 'synthetic-admin-' + uuid.uuid4().hex, 'DEMO_ACCESS_TOKEN': service_token,
            'CHAT_RUNTIME_MODE': 'mock', 'STORE_MODE': 'postgres', 'DATABASE_URL': chat_db,
            'INTERNAL_URL': chat_url, 'COOKIE_SECURE': 'false', 'CHAT_MEDIA_ROOT': str(media)})
        with httpx.Client(timeout=30, trust_env=False) as http:
            def ready_json(url, check=lambda data: True):
                response = http.get(url)
                return response.json() if response.status_code == 200 and check(response.json()) else None
            until(lambda: http.get(callback_url+'/health').status_code==200)
            search_ready = until(lambda: ready_json(search_url+'/readyz'))
            profile_ready = until(lambda: ready_json(profile_url+'/health', lambda data: data['catalog']['active'] and data['store']['connected']))
            auth = {'Authorization': 'Bearer '+service_token}
            until(lambda: http.get(chat_url+'/readyz', headers=auth).status_code==200)
            assert profile_ready['catalog']['version']==binding['catalogVersion']
            result['searchSnapshotId'] = snapshot = search_ready['snapshotId']
            passed('three_real_processes_ready_same_postgres_and_catalog_version')
            metadata = http.get(search_url+'/v1/metadata').json()
            assert metadata['snapshotId']==snapshot
            hits = http.post(search_url+'/v1/search', json={'query':'휴대용 컵', 'snapshotId':snapshot, 'filters':{'availability':'available_or_unknown'}}, headers={'X-Search-Source':'chat'})
            assert hits.status_code==200, hits.text
            assert {p['productId'] for p in hits.json()['hits']} <= expected_ids
            assert http.post(search_url+'/v1/products', json={'ids':[first_id], 'snapshotId':'invalid'}).status_code==409
            product = http.post(search_url+'/v1/products', json={'ids':[first_id], 'snapshotId':snapshot}).json()['products'][0]
            assert product['image'].startswith('/media/')
            assert http.get(search_url+product['image']).content==(media/Path(product['image']).name).read_bytes()
            assert http.get(chat_url+product['image']).content==(media/Path(product['image']).name).read_bytes()
            if not args.real_catalog:
                with psycopg.connect(url) as db:
                    assert db.execute('SELECT image_asset_id,source_image_url FROM ai_catalog.products WHERE id=101').fetchone()==('synthetic-image-101','https://example.invalid/source-101.png')
            passed('image_url_and_asset_metadata_preserved_without_binary_or_download')
            passed('hybrid_search_ids_snapshot_and_stale_snapshot_rejection')
            qa = http.post(search_url+'/api/search', json={'query':'합성 텀블러'})
            assert qa.status_code==200
            with psycopg.connect(url) as db:
                assert db.execute('SELECT count(*) FROM ai_search.searches').fetchone()[0]>0
            passed('search_qa_in_shared_postgres_without_sqlite_or_paid_budget')
            with psycopg.connect(url) as db:
                db.execute('UPDATE ai_catalog.products SET unit_price=unit_price+1 WHERE id=%s',(first_id,))
            until(lambda: http.get(search_url+'/readyz').status_code==503, timeout=10)
            assert http.get(search_url+'/v1/metadata').status_code==503
            with psycopg.connect(url) as db:
                db.execute('UPDATE ai_catalog.products SET unit_price=unit_price-1 WHERE id=%s',(first_id,))
            until(lambda: http.get(search_url+'/readyz').status_code==200, timeout=10)
            passed('canonical_product_vector_mismatch_rejected_and_recovery')
            def status(rid):
                with psycopg.connect(url) as db:
                    row=db.execute('SELECT status FROM ai_profile.profile_runs WHERE recipient_user_id=%s', (rid,)).fetchone()
                    return row[0] if row else None
            def request(rid):
                return {'recipientUserId':rid,'sourceVersion':1,'giftPreference':'합성 휴대용 선물','dislikedCategories':[], 'reviews':[]}
            path = profile_url+'/api/internal/v1/ai/profile/extract-and-pool'
            assert http.post(path,json=request(901)).status_code==401
            assert http.post(path,json=request(901),headers=auth).status_code==202
            until(lambda:status(901)=='DELIVERED')
            with psycopg.connect(url) as db:
                saved=db.execute('SELECT recommended_product_ids FROM ai_profile.recipient_profiles WHERE recipient_user_id=901').fetchone()
                assert saved and set(saved[0]) <= expected_ids
            passed('profiling_auth_result_saved_and_callback_delivered')
            http.put(callback_url+'/fixture/mode/902/503')
            assert http.post(path,json=request(902),headers=auth).status_code==202
            until(lambda:status(902)=='RESULT_READY')
            http.put(callback_url+'/fixture/mode/902/200')
            assert http.post(path,json=request(902),headers=auth).status_code==202
            until(lambda:status(902)=='DELIVERED')
            calls=http.get(callback_url+'/fixture/calls').json()['902'];assert len(calls)==2 and calls[0]==calls[1]
            passed('profiling_callback_failure_retains_result_same_payload_retry')
            http.put(callback_url+'/fixture/mode/903/400')
            assert http.post(path,json=request(903),headers=auth).status_code==202
            until(lambda:status(903)=='FAILED')
            http.put(callback_url+'/fixture/mode/903/200')
            assert http.post(path,json=request(903),headers=auth).status_code==202
            until(lambda:status(903)=='DELIVERED')
            passed('profiling_failed_result_persisted_then_same_request_recovery')
            # Real chat PostgreSQL events, fixture-only HTTP and streaming.
            cid,tid=str(uuid.uuid4()),str(uuid.uuid4());base=chat_url+'/v1/chat/test/conversations/'+cid
            assert http.put(base,json={'senderUserId':7,'recipientUserId':42},headers=auth).status_code==201
            turn=base+'/turns/'+tid
            assert http.put(turn,json={'message':'/mock reconnect'},headers=auth).status_code==202
            cursor=0
            with http.stream('GET',turn+'/events',headers=auth) as stream:
                for line in stream.iter_lines():
                    if line.startswith('id: '):cursor=int(line[4:])
                    if line.startswith('data: ') and cursor>=2:break
            assert http.get(turn,headers=auth).json()['data']['status']=='PROCESSING'
            with http.stream('GET',turn+'/events',params={'afterEventId':cursor},headers=auth) as stream:
                replay='\n'.join(stream.iter_lines())
            assert 'event: turn.finished' in replay and f'id: {cursor}\n' not in replay
            assert http.put(turn,json={'message':'/mock reconnect'},headers=auth).status_code==200
            tid=str(uuid.uuid4());turn=base+'/turns/'+tid
            assert http.put(turn,json={'message':'/mock slow'},headers=auth).status_code==202
            assert http.put(turn+'/cancellation',headers=auth).json()['data']['status']=='CANCELLED'
            assert http.put(turn+'/cancellation',headers=auth).json()['data']['status']=='CANCELLED'
            assert http.get(turn+'/report',headers=auth).json()['metrics']['modelCalls']==0
            passed('chat_sse_disconnect_cursor_reconnect_idempotency_and_cancel')
            for role,query in [('ai_app','UPDATE ai_catalog.products SET unit_price=0'),('ai_search_service','UPDATE ai_profile.recipient_profiles SET source_version=0'),('ai_chat_service','UPDATE ai_catalog.products SET unit_price=0')]:
                try:
                    with psycopg.connect(url.replace('fixture_owner@',role+'@')) as db:db.execute(query)
                except psycopg.errors.InsufficientPrivilege:
                    continue
                raise AssertionError('Cross-service write permission unexpectedly allowed')
            passed('schema_write_ownership_and_catalog_read_only_roles')
            # Exercise the actual chat HTTP adapter against the separate search.
            code='''import asyncio, json, os
from app.search_client import SearchClient
async def check():
 async with SearchClient(os.environ['FIXTURE_SEARCH_URL'],source='chat') as client:
  m=await client.metadata()
  result=await client.search({'query':'컵'},snapshot_id=m['snapshotId'])
  p=await client.get_products([int(os.environ['FIXTURE_PRODUCT_ID'])],snapshot_id=m['snapshotId'])
  assert result['snapshotId']==m['snapshotId'] and p['products']
  print(json.dumps({'chatSearchClientContractPassed':True}))
asyncio.run(check())
'''
            run([python['chat'],'-B','-c',code],cwd=ROOT/'chat',variables={'FIXTURE_SEARCH_URL':search_url,'FIXTURE_PRODUCT_ID':str(first_id)})
            passed('actual_chat_searchclient_http_boundary_same_snapshot')
            if not args.real_catalog:
                profile_check = run([python['profiling'], '-B', str(ROOT/'integration/tests/check_real_profile_embedding.py'), '--runtime', str(runtime)],
                                    variables={'FIXTURE_PROFILE_DB':profile_db.replace('postgresql://','postgresql+psycopg://')})
                result['realEmbeddingMMR'] = json.loads(profile_check.stdout)
                passed('synthetic_tags_real_db_vectors_query_encoder_percentile_and_mmr')
                tools_check = run([python['chat'], '-B', str(ROOT/'integration/tests/check_chat_tools_search.py')],
                                  variables={'FIXTURE_SEARCH_URL':search_url,'FIXTURE_PROFILE_RESULT':profile_check.stdout})
                result['chatToolIntegration'] = json.loads(tools_check.stdout)
                passed('fake_provider_actual_gift_tool_search_and_transferred_profiler_pool')
                changed = json.loads((ROOT/'integration/tests/fixture.json').read_text())
                changed['generation'] = 'fixture-catalog-v2'
                changed['categories'][1]['parent'] = None
                changed['categories'][2]['parent'] = 'CAT-CUP'
                for fixture_product in changed['products']:
                    fixture_product['category'] = 'CAT-AUDIO'
                changed['products'][0]['views'] = 5000
                changed['products'][0]['price'] = 15000
                fixture_v2 = scratch/'fixture-v2.json'
                fixture_v2.write_text(json.dumps(changed))
                published = run([python['product-search'],str(ROOT/'integration/catalog.py'),str(runtime),'--fixture',str(fixture_v2)], variables={'CATALOG_OWNER_DATABASE_URL':url})
                version_v2 = json.loads(published.stdout)['catalogVersion']
                with psycopg.connect(url) as connection:
                    assert connection.execute("SELECT c.parent_id,c.level FROM ai_catalog.categories c JOIN ai_catalog.integration_category_sources m ON m.category_id=c.id WHERE m.source_category_id='CAT-CUP'").fetchone()==(None,1)
                    assert connection.execute("SELECT m.source_category_id,p.view_count FROM ai_catalog.products p JOIN ai_catalog.integration_category_sources m ON m.category_id=p.category_id WHERE p.id=101").fetchone()==('CAT-AUDIO',5000)
                refreshed = until(lambda: ready_json(search_url+'/readyz',lambda data:data['snapshotId']!=snapshot))
                assert http.post(search_url+'/v1/products',json={'ids':[101],'snapshotId':snapshot}).status_code==409
                assert http.get(profile_url+'/health').json()['catalog']['active'] is False
                assert http.post(path,json=request(905),headers=auth).status_code==503
                profile_process.terminate();profile_process.wait(timeout=10)
                profile_process = start('profiling-v2',service_cmd('profiling','integration.profiler_free_runtime:app',profile_port),ROOT/'profiling',profile_variables)
                until(lambda: ready_json(profile_url+'/health',lambda data:data['catalog']['active'] and data['catalog']['version']==version_v2))
                assert http.post(path,json=request(905),headers=auth).status_code==202
                until(lambda:status(905)=='DELIVERED')
                result['catalogActivation']={'oldSnapshot':snapshot,'newSnapshot':refreshed['snapshotId'],'newVersion':version_v2,'staleProfilerHealthActive':False,'staleProfilerIntakeStatus':503,'profilerRestartRecovered':True}
                passed('v1_to_v2_category_popularity_snapshot_and_profiler_intake_restart_recovery')
                bad = json.loads(run([python['product-search'],'-B',str(ROOT/'integration/tests/publish_bad_generation.py')],variables={'CATALOG_OWNER_DATABASE_URL':url}).stdout)
                until(lambda:http.get(search_url+'/readyz').status_code==503)
                rejected = []
                for _ in range(3):
                    time.sleep(1.1)
                    rejected.append(http.get(search_url+'/readyz').status_code)
                    assert http.get(search_url+'/v1/metadata').status_code==503
                assert rejected==[503,503,503]
                assert http.get(profile_url+'/health').json()['catalog']['active'] is False
                with psycopg.connect(url) as connection:
                    connection.execute('UPDATE ai_catalog.catalog_versions SET is_active=false WHERE is_active')
                    connection.execute('UPDATE ai_catalog.catalog_versions SET is_active=true WHERE id=%s',(bad['restoreCatalogVersion'],))
                until(lambda:ready_json(search_url+'/readyz',lambda data:data['snapshotId']==refreshed['snapshotId']))
                until(lambda:ready_json(profile_url+'/health',lambda data:data['catalog']['active']))
                result['failedGenerationRepeatedPoll']={'readyStatuses':rejected,'metadataStatuses':[503]*3,'restoredKnownGoodSnapshot':True}
                passed('failed_encoder_generation_repeated_poll_is_unready_and_known_good_reactivation_recovers')
            if args.browser:
                ui_output=args.output.with_suffix('.ui.json')
                run([str(ROOT/'integration/.venv/bin/python'),str(ROOT/'integration/tests/check_ui.py'),'--url',search_url,'--browser',str(args.browser),'--output',str(ui_output)])
                result['browserSample']=json.loads(ui_output.read_text())
            if args.real_catalog:
                # Full product lookup, not a sample of the catalog.
                ids=sorted(expected_ids); looked_up=set()
                for start_at in range(0,len(ids),100):
                    response=http.post(search_url+'/v1/products',json={'ids':ids[start_at:start_at+100],'snapshotId':snapshot})
                    assert response.status_code==200,response.text
                    looked_up.update(p['productId'] for p in response.json()['products'])
                assert looked_up==expected_ids
                result['fullProductLookup']={'total':len(ids),'success':len(looked_up),'missing':0,'failed':0}
                from concurrent.futures import ThreadPoolExecutor
                import threading
                clients=threading.local(); opened_clients=[]
                def get_media(path):
                    try:
                        if not hasattr(clients,'http'):
                            clients.http=httpx.Client(timeout=60,trust_env=False)
                            opened_clients.append(clients.http)
                        response=clients.http.get(search_url+'/media/'+path.name)
                        ok=response.status_code==200 and hashlib.sha256(response.content).hexdigest()==hashlib.sha256(path.read_bytes()).hexdigest()
                        return {'ok':ok,'status':response.status_code}
                    except httpx.HTTPError as error:
                        return {'ok':False,'errorClass':type(error).__name__}
                files=[p for p in media.iterdir() if p.is_file()]
                with ThreadPoolExecutor(max_workers=4) as pool:
                    checked=list(pool.map(get_media,files))
                for client in opened_clients:client.close()
                from collections import Counter
                successes=sum(i['ok'] for i in checked)
                result['allExistingMediaHTTP']={'total':len(files),'success':successes,'failed':len(files)-successes,'hotlinkRequests':0,'failureKinds':dict(Counter(i.get('errorClass',str(i.get('status'))) for i in checked if not i['ok'])),'reusedBoundedHTTPConnections':True}
                assert successes==len(files)
                result['allRequiredImagesComplete']=imported['mediaComplete']
                passed('all4231_product_lookups_and_every_existing_media_http200_hash')
        result['passed']=True
    except Exception as exc:
        result.update(passed=False, errorClass=type(exc).__name__, error=str(exc), scratch=str(scratch))
        raise
    finally:
        for process in reversed(children):
            process.terminate()
        for process in children:
            try:process.wait(timeout=20)
            except subprocess.TimeoutExpired:result['cleanupIncomplete']=True
        if pg_started:
            try:run([str(args.pg_bin/'pg_ctl'),'-D',str(scratch/'pg'),'-m','smart','-w','stop'])
            except Exception:result['cleanupIncomplete']=True
        for log in logs:log.close()
        result['temporaryProcessesAndDBStopped']=not result.get('cleanupIncomplete',False)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
