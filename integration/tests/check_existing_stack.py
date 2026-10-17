"""Contracts against an explicitly marked local test DB and running services.

No model calls or lifecycle changes. The only catalog fault is restored in finally.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

import httpx
import psycopg


def check(args):
    endpoints = json.loads(args.endpoints.read_text())
    env = dict(line.split('=', 1) for line in (args.endpoints.parent / 'chat.env').read_text().splitlines())
    token = env['AI_SERVICE_TOKEN']
    if not token.startswith('synthetic-'):
        raise ValueError('Only synthetic fixture credentials are accepted')
    urls = {key: 'http://127.0.0.1:'+str(port) for key, port in endpoints['ports'].items()}
    database = 'postgresql://fixture_owner@'+endpoints['databaseIP']+':5432/postgres'
    callback = 'http://'+endpoints['callbackIP']+':8080'
    expected_ids = {p['id'] for p in json.loads(args.catalog.read_text())['products']}
    result = {'passed':False, 'tests':[], 'paidModelCalls':0, 'existingProductionServicesChanged':False}
    def passed(name):
        result['tests'].append(name)
    def until(fn):
        deadline = time.monotonic()+30
        while time.monotonic()<deadline:
            value = fn()
            if value:
                return value
            time.sleep(.1)
        raise AssertionError('Local fixture wait timed out')
    def status(rid):
        with psycopg.connect(database) as db:
            row = db.execute('SELECT status FROM ai_profile.profile_runs WHERE recipient_user_id=%s',(rid,)).fetchone()
        return row[0] if row else None
    with psycopg.connect(database) as db:
        assert db.execute('SELECT current_user').fetchone()[0] == 'fixture_owner'
        assert db.execute('SELECT count(*) FROM ai_catalog.integration_marker').fetchone()[0] == 1
        version = str(db.execute('SELECT id FROM ai_catalog.catalog_versions WHERE is_active').fetchone()[0])
        assert version == args.catalog_version
        assert db.execute('SELECT count(*) FROM ai_catalog.products').fetchone()[0] == len(expected_ids)
        assert db.execute("SELECT count(*) FROM ai_catalog.product_media WHERE kind='detail'").fetchone()[0] == 0
    passed('isolated_marked_single_catalog_and_detail_exclusion')
    with httpx.Client(timeout=60, trust_env=False) as http:
        auth = {'Authorization':'Bearer '+token}
        ready = http.get(urls['search']+'/readyz'); assert ready.status_code==200
        snapshot = ready.json()['snapshotId']; result['snapshotId']=snapshot
        profile = http.get(urls['profiling']+'/health').json()
        assert profile['catalog']['active'] and profile['catalog']['version']==version and profile['store']['connected']
        chat = http.get(urls['chat']+'/readyz', headers=auth)
        assert chat.status_code==200 and chat.json()['mode']=='mock'
        passed('three_containers_ready_same_canonical_database')
        hits = http.post(urls['search']+'/v1/search',json={'query':'컵','snapshotId':snapshot},headers={'X-Search-Source':'chat'})
        assert hits.status_code==200 and {p['productId'] for p in hits.json()['hits']} <= expected_ids
        assert http.post(urls['search']+'/v1/products',json={'ids':[min(expected_ids)],'snapshotId':'invalid'}).status_code==409
        passed('hybrid_search_and_stale_snapshot_rejection')
        looked_up = set()
        ids = sorted(expected_ids)
        for index in range(0,len(ids),100):
            response = http.post(urls['search']+'/v1/products',json={'ids':ids[index:index+100],'snapshotId':snapshot})
            assert response.status_code==200
            looked_up.update(p['productId'] for p in response.json()['products'])
        assert looked_up==expected_ids
        result['fullProductLookup']={'total':len(ids),'success':len(looked_up),'missing':0}
        passed('all_products_lookup_in_container')
        first = min(expected_ids)
        with psycopg.connect(database) as db:
            original_price = db.execute('SELECT unit_price FROM ai_catalog.products WHERE backend_product_id=%s',(first,)).fetchone()[0]
            db.execute('UPDATE ai_catalog.products SET unit_price=unit_price+1 WHERE backend_product_id=%s',(first,))
        try:
            until(lambda:http.get(urls['search']+'/readyz').status_code==503)
            assert http.get(urls['search']+'/v1/metadata').status_code==503
        finally:
            with psycopg.connect(database) as db:
                db.execute('UPDATE ai_catalog.products SET unit_price=%s WHERE backend_product_id=%s',(original_price,first))
        until(lambda:http.get(urls['search']+'/readyz').status_code==200)
        passed('catalog_hash_fault_rejected_and_original_price_restored')
        path = urls['profiling']+'/api/internal/v1/ai/profile/extract-and-pool'
        def request(rid):
            return {'recipientUserId':rid,'sourceVersion':1,'giftPreference':'합성 컵','dislikedCategories':[],'reviews':[]}
        assert http.post(path,json=request(9901)).status_code==401
        assert http.post(path,json=request(9901),headers=auth).status_code==202
        until(lambda:status(9901)=='DELIVERED')
        passed('profile_auth_and_result_callback_in_containers')
        http.put(callback+'/fixture/mode/9902/503')
        assert http.post(path,json=request(9902),headers=auth).status_code==202
        until(lambda:status(9902)=='RESULT_READY')
        http.put(callback+'/fixture/mode/9902/200')
        assert http.post(path,json=request(9902),headers=auth).status_code==202
        until(lambda:status(9902)=='DELIVERED')
        calls = http.get(callback+'/fixture/calls').json()['9902']
        assert len(calls)==2 and calls[0]==calls[1]
        passed('profile_callback_failure_same_payload_retry')
        cid, tid = str(uuid.uuid4()),str(uuid.uuid4())
        base = urls['chat']+'/v1/chat/test/conversations/'+cid
        assert http.put(base,json={'senderUserId':7,'recipientUserId':42},headers=auth).status_code==201
        turn = base+'/turns/'+tid
        assert http.put(turn,json={'message':'/mock reconnect'},headers=auth).status_code==202
        cursor = 0
        with http.stream('GET',turn+'/events',headers=auth) as stream:
            for line in stream.iter_lines():
                if line.startswith('id: '): cursor=int(line[4:])
                if line.startswith('data: ') and cursor>=2: break
        assert http.get(turn,headers=auth).json()['data']['status']=='PROCESSING'
        with http.stream('GET',turn+'/events',params={'afterEventId':cursor},headers=auth) as stream:
            replay='\n'.join(stream.iter_lines())
        assert 'event: turn.finished' in replay and f'id: {cursor}\n' not in replay
        assert http.put(turn,json={'message':'/mock reconnect'},headers=auth).status_code==200
        slow=base+'/turns/'+str(uuid.uuid4())
        assert http.put(slow,json={'message':'/mock slow'},headers=auth).status_code==202
        for _ in range(2):
            assert http.put(slow+'/cancellation',headers=auth).json()['data']['status']=='CANCELLED'
        assert http.get(slow+'/report',headers=auth).json()['metrics']['modelCalls']==0
        passed('container_chat_stored_sse_cursor_replay_idempotency_and_cancel')
    files=[p for p in args.media.iterdir() if p.is_file()]
    local=threading.local(); clients=[]
    def media(path):
        if not hasattr(local,'http'):
            local.http=httpx.Client(timeout=60,trust_env=False);clients.append(local.http)
        try:
            response=local.http.get(urls['search']+'/media/'+path.name)
            return response.status_code==200 and hashlib.sha256(response.content).digest()==hashlib.sha256(path.read_bytes()).digest()
        except httpx.HTTPError:
            return False
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            success=sum(pool.map(media,files))
    finally:
        for client in clients: client.close()
    result['allExistingMediaHTTP']={'total':len(files),'success':success,'failed':len(files)-success,'hotlinkRequests':0}
    assert success==len(files)
    passed('every_existing_media_http200_and_hash_in_container')
    result.update(passed=True, catalogVersion=version)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['endpoints','catalog','media','output']:
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--catalog-version',required=True)
    args=parser.parse_args()
    result=check(args)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
