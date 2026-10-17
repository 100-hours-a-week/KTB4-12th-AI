"""Native loopback scenario using the candidate's real service modules and CPU encoder.

All requests/products are synthetic. Chat mock transport, Profiler tagger off.
Never claims paid-model quality, final Linux image or real BE integration.
"""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from uuid import uuid4

import httpx

from integration.search_sync import validated_fetcher
from tools.sync_postgres import sync

ROOT=Path(__file__).resolve().parents[2]


def run(database, source, *, live=False, live_options=None):
    output=Path(os.environ['SINGLE_SERVER_TEST_OUTPUT'])/('http-'+database['migrationMode']);output.mkdir()
    environments=Path(os.environ['SINGLE_SERVER_TEST_ENVIRONMENTS'])
    assets=Path(os.environ['SINGLE_SERVER_TEST_ENCODER_ASSETS'])
    runtime,export,categories,seal,_,_=source
    for name in ('service.mjs','runtime.mjs','documents.mjs'):
        shutil.copyfile(ROOT/'product-search/embedding'/name,runtime/'embedding'/name)
    for name in ('models','node_modules'):(runtime/'embedding'/name).symlink_to(assets/name,target_is_directory=True)
    callbacks=[];blocked=threading.Event();release=threading.Event();mode={'fail':False,'slow':False}
    token='synthetic-single-server-contract'
    class Backend(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def authorized(self):return self.headers.get('Authorization')=='Bearer '+token
        def do_GET(self):
            if not self.authorized():self.send_error(401);return
            if mode['fail']:self.send_error(503);return
            if mode['slow']:blocked.set();release.wait(15)
            body=export if self.path=='/export' else categories
            raw=json.dumps(body).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
        def do_POST(self):
            if not self.authorized():self.send_error(401);return
            callbacks.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200);self.end_headers();self.wfile.write(b'{}')
    backend=ThreadingHTTPServer(('127.0.0.1',0),Backend)
    backend_thread=threading.Thread(target=backend.serve_forever,daemon=True);backend_thread.start()
    be=f'http://127.0.0.1:{backend.server_port}'
    fetch=validated_fetcher(be+'/export',be+'/categories',token)
    clean={k:v for k,v in os.environ.items() if not any(x in k for x in ('TOKEN','SECRET','PASSWORD','DATABASE_URL','API_KEY'))
           and not k.startswith(('PROFILING_','SEARCH_','CHAT_','STORE_','BUDGET_'))}
    clean.update(PYTHONDONTWRITEBYTECODE='1',HF_HUB_OFFLINE='1',OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
    processes=[];logs=[]
    def start(name,service,module,env):
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        log=(output/(name+'.log')).open('ab');logs.append(log)
        command=[str(environments/service/'.venv/bin/python'),'-B','-m','uvicorn',module,'--host','127.0.0.1','--port',str(port),'--no-access-log']
        p=subprocess.Popen(command,cwd=ROOT/service,env=clean|env,stdout=log,stderr=subprocess.STDOUT);processes.append(p)
        return p,f'http://127.0.0.1:{port}'
    def wait(http,url,process):
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            assert process.poll() is None, 'Service exited; inspect synthetic scenario log'
            try:
                response=http.get(url+'/readyz')
                if response.status_code==200:return
            except httpx.HTTPError:pass
            time.sleep(.1)
        raise AssertionError('Service readiness timeout')
    result={}
    try:
        initial=sync(runtime,database['sync'],fetch,apply=True)
        with httpx.Client(headers={'Authorization':'Bearer '+token},timeout=10,trust_env=False) as http:
            search,search_url=start('search','product-search','app:app',{
                'PYTHONPATH':os.pathsep.join(map(str,[ROOT/'product-search',ROOT/'profiling/src',ROOT])),
                'SEARCH_RUNTIME_ROOT':str(runtime),'SEARCH_CATALOG_DATABASE_URL':database['search'],
                'SEARCH_QA_DATABASE_URL':database['search'],'SEARCH_SERVICE_TOKEN':token})
            wait(http,search_url,search)
            contract=http.get(search_url+'/v1/catalog/contract').json()
            assert contract['catalogVersion']==initial['catalogVersion'] and contract['modelRevision']
            q=http.post(search_url+'/v1/embeddings/query',json={'snapshotId':contract['snapshotId'],'texts':['합성 컵']})
            assert q.status_code==200 and len(q.json()['vectors'][0])==768
            query=q.json()['vectors'][0]
            pure=http.post(search_url+'/v1/vector-search',json={'snapshotId':contract['snapshotId'],'vector':query,'limit':2})
            assert pure.status_code==200 and len(pure.json()['hits'])==2
            if live:
                from integration.tests.live_single_server import run_models
                result=run_models(database,export,contract,search_url,be,callbacks,token,output,start,wait,**(live_options or {}))
                return result
            mode['fail']=True
            try:sync(runtime,database['sync'],fetch,apply=True)
            except ValueError:pass
            else:raise AssertionError('BE 503 accepted')
            mode['fail']=False
            assert http.get(search_url+'/v1/catalog/contract').json()==contract
            export['data']['generatedAt']='2026-10-16T02:00:00Z';export['data']['products'][0]['price']=999;seal()
            mode['slow']=True;errors=[];synced=[]
            def slow_sync():
                try:synced.append(sync(runtime,database['sync'],fetch,apply=True))
                except Exception as e:errors.append(type(e).__name__)
            worker=threading.Thread(target=slow_sync);worker.start();assert blocked.wait(3)
            before=time.monotonic();response=http.post(search_url+'/v1/search',json={'query':'컵','limit':1});elapsed=time.monotonic()-before
            assert response.status_code==200 and len(response.json()['hits'])==1 and elapsed<3
            release.set();worker.join(30);assert not worker.is_alive() and not errors;mode['slow']=False
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                updated=http.get(search_url+'/v1/catalog/contract')
                if updated.status_code==200 and updated.json()['catalogVersion']==synced[0]['catalogVersion']:break
                time.sleep(.1)
            else:raise AssertionError('Serving Search did not activate pending generation')
            # The cache is disposable: a fresh process must reconstruct the
            # same active snapshot from PG without the prior CURRENT/files.
            active_contract=updated.json()
            search.terminate();search.wait(timeout=15)
            shutil.rmtree(runtime/'data')
            search,search_url=start('search-restart','product-search','app:app',{
                'PYTHONPATH':os.pathsep.join(map(str,[ROOT/'product-search',ROOT/'profiling/src',ROOT])),
                'SEARCH_RUNTIME_ROOT':str(runtime),'SEARCH_CATALOG_DATABASE_URL':database['search'],
                'SEARCH_QA_DATABASE_URL':database['search'],'SEARCH_SERVICE_TOKEN':token})
            wait(http,search_url,search)
            assert http.get(search_url+'/v1/catalog/contract').json()==active_contract
            profile,profile_url=start('profiler','profiling','integration.profiler_free_runtime:app',{
                'PYTHONPATH':os.pathsep.join(map(str,[ROOT/'profiling/src',ROOT,ROOT/'product-search'])),
                'PROFILING_DATABASE_URL':database['profiler'].replace('postgresql://','postgresql+psycopg://'),
                'PROFILING_APP_DB_ROLE':'ai_profiler_service','PROFILING_SERVICE_TOKEN':token,
                'PROFILING_BACKEND_BASE_URL':be,'PROFILING_TAGGER':'none','PROFILING_TAG_SPEC':'tag3disliked',
                'PROFILING_CATALOG_SOURCE':'search_http','PROFILING_SEARCH_BASE_URL':search_url,
                'PROFILING_SEARCH_TOKEN':token,'PROFILING_STRICT_STARTUP':'true'})
            wait(http,profile_url,profile)
            body={'recipientUserId':777,'sourceVersion':1,'giftPreference':'합성 컵 선물','dislikedCategories':[],'reviews':[]}
            endpoint=profile_url+'/api/internal/v1/ai/profile/extract-and-pool'
            assert http.post(endpoint,json=body).status_code==202
            deadline=time.monotonic()+15
            while not callbacks and time.monotonic()<deadline:time.sleep(.05)
            assert callbacks
            first=copy.deepcopy(callbacks[0]);assert http.post(endpoint,json=body).status_code==202
            deadline=time.monotonic()+10
            while len(callbacks)<2 and time.monotonic()<deadline:time.sleep(.05)
            assert callbacks==[first,first]
            chat,chat_url=start('chat','chat','app.main:app',{
                'PYTHONPATH':os.pathsep.join(map(str,[ROOT/'chat',ROOT])),
                'STORE_MODE':'postgres','DATABASE_URL':database['chat']+'?options=-csearch_path%3Dai_chat,public',
                'CHAT_RUNTIME_MODE':'mock','AI_SERVICE_TOKEN':token,'SESSION_SECRET':token,'QA_ADMIN_PASSWORD':token,
                'DEMO_ACCESS_TOKEN':token,'COOKIE_SECURE':'false'})
            wait(http,chat_url,chat)
            cid,tid=str(uuid4()),str(uuid4());base=chat_url+'/v1/chat/conversations/'+cid
            assert http.put(base,json={'senderUserId':11,'recipientUserId':777}).status_code==201
            assert http.put(base+'/turns/'+tid,json={'message':'/mock cards'}).status_code in (200,202)
            stream=http.get(base+'/turns/'+tid+'/events?afterEventId=0');assert stream.status_code==200 and 'event: turn.finished' in stream.text
            saved=http.get(base+'/turns/'+tid).json()['data'];assert saved['status']=='COMPLETED'
            assert http.put(base+'/turns/'+tid,json={'message':'/mock cards'}).json()['data']==saved
            assert http.put(base,json={'senderUserId':12,'recipientUserId':777}).status_code==409
            assert http.put(base+'/turns/'+tid+'/cancellation').json()['data']==saved
            # Restart Chat and recover the persisted SSE/result with the same ID.
            chat.terminate();chat.wait(timeout=15)
            chat2,chat_url2=start('chat-restart','chat','app.main:app',{
                'PYTHONPATH':os.pathsep.join(map(str,[ROOT/'chat',ROOT])),
                'STORE_MODE':'postgres','DATABASE_URL':database['chat']+'?options=-csearch_path%3Dai_chat,public',
                'CHAT_RUNTIME_MODE':'mock','AI_SERVICE_TOKEN':token,'SESSION_SECRET':token,'QA_ADMIN_PASSWORD':token,
                'DEMO_ACCESS_TOKEN':token,'COOKIE_SECURE':'false'})
            wait(http,chat_url2,chat2)
            assert http.put(chat_url2+'/v1/chat/conversations/'+cid+'/turns/'+tid,json={'message':'/mock cards'}).json()['data']==saved
            result={'passed':True,'synthetic':True,'actualCPUEncoder':True,'pgCanonicalGeneration':True,
                    'searchDuringBlockedSyncSeconds':elapsed,'beFetchFailurePreservesGeneration':True,
                    'pureVectorLimit2':True,'hybridLimit1':True,'profileDuplicateCallbackIdentical':True,
                    'chatStoredSseRetryCancellationOwnershipRestart':True,'profileTagger':'none',
                    'searchRestartAfterCacheLoss':True,
                    'chatMode':'mock','paidCalls':0,'realBEConnected':False,'linuxImagesVerified':False}
    finally:
        release.set()
        for p in reversed(processes):
            if p.poll() is None:p.terminate()
        for p in reversed(processes):
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
        backend.shutdown();backend.server_close();backend_thread.join(timeout=3)
        for log in logs:log.close()
        result['temporaryProcessesStopped']=all(p.poll() is not None for p in processes)
        (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result
