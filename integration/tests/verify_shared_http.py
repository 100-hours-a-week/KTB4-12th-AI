"""Loopback synthetic BE export -> separate sync -> Search -> guarded Profiler."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

import httpx
import numpy as np
import psycopg
import uvicorn
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[2]
PG=Path('/opt/homebrew/opt/postgresql@18/bin')
sys.path.insert(0,str(ROOT/'product-search'))
from tools.sync_catalog import sync, fetcher
from search.snapshots import active_data_dir
from search.service import SearchService
from search.reloading import ReloadingSearch


class SyntheticEncoder:
    async def get(self,text,source):
        vector=np.zeros(768,dtype=np.float32);vector[0]=1
        return vector,False


def embed(root,texts,directory):
    vectors=np.zeros((len(texts),768),dtype=np.float32)
    for i,text in enumerate(texts):
        vectors[i,0]=1;vectors[i,1+int(hashlib.sha256(text.encode()).hexdigest()[:4],16)%767]=.1
    return vectors,[10]*len(texts)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--database-proof', type=Path, required=True, help='Successful owned migration verification directory; never an existing project DB')
    parser.add_argument('--recipient-id', type=int, default=4243, help='Unused synthetic recipient in this disposable cluster')
    args=parser.parse_args()
    OUT=args.output.resolve();proof=args.database_proof.resolve()
    verification=(ROOT.parent/'verification').resolve()
    if not OUT.is_relative_to(verification) or not proof.is_relative_to(verification):
        raise ValueError('Only this candidate verification root is allowed')
    evidence=json.loads((proof/'result.json').read_text())
    if not evidence.get('passed') or not evidence.get('synthetic') or not evidence.get('temporaryClusterStopped'):
        raise ValueError('Successful stopped synthetic migration proof required')
    cluster=proof/'cluster'
    if not (cluster/'PG_VERSION').is_file():raise RuntimeError('Run owned migration verification first')
    if OUT.exists():raise RuntimeError('Use a fresh verification directory')
    OUT.mkdir();runtime=OUT/'runtime';(runtime/'embedding').mkdir(parents=True)
    shutil.copyfile(ROOT/'product-search/embedding/model.lock.json',runtime/'embedding/model.lock.json')
    for key in list(os.environ):
        if any(x in key for x in ('TOKEN','SECRET','PASSWORD','API_KEY','DATABASE_URL')) or key.startswith('PROFILING_'):
            os.environ.pop(key)
    os.environ['SEARCH_SERVICE_TOKEN']='synthetic-contract'
    export=json.loads((ROOT/'product-search/examples/products-export.json').read_text())
    categories=json.loads((ROOT/'product-search/examples/categories.json').read_text())
    for i,p in enumerate(export['data']['products']):p['viewCount']=50-i
    callbacks=[];blocked=threading.Event();release=threading.Event();mode={'fail':False,'slow':False}
    class BE(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.headers.get('Authorization')!='Bearer synthetic-contract':
                self.send_error(401);return
            if mode['fail']:self.send_error(503);return
            if mode['slow']:
                blocked.set();release.wait(10)
            value=export if self.path=='/export' else categories
            raw=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
        def do_POST(self):
            callbacks.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200);self.end_headers();self.wfile.write(b'{}')
    backend=ThreadingHTTPServer(('127.0.0.1',0),BE);threading.Thread(target=backend.serve_forever,daemon=True).start()
    base=f'http://127.0.0.1:{backend.server_port}'
    fetch=fetcher(base+'/export',base+'/categories','synthetic-contract')
    sockdir=Path(tempfile.mkdtemp(prefix='shared-http-db-',dir='/tmp'))
    started=False;server=None;thread=None;result={}
    services=[];search_loop=[]
    try:
        first=sync(runtime,fetch,embed=embed,fingerprint=lambda *_:'f'*64,apply=True)
        service=SearchService(active_data_dir(runtime/'data'),SyntheticEncoder());services.append(service)
        owner=ReloadingSearch(runtime,service,service.embedding,encoder_fingerprint='f'*64)
        def load_synthetic(directory):
            value=SearchService(directory,SyntheticEncoder());services.append(value);return value
        owner._load_candidate=load_synthetic  # Explicit synthetic encoder; real encoder parity is a separate verification.
        import app as search_app
        @asynccontextmanager
        async def synthetic_lifespan(app):
            app.state.search=owner;app.state.ready=True;app.state.active_searches=0;app.state.active_profile_searches=0
            search_loop.append(asyncio.get_running_loop())
            try:yield
            finally:await owner.close()
        search_app.app.router.lifespan_context=synthetic_lifespan
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(search_app.app,log_level='error'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline:time.sleep(.01)
        assert server.started
        search_url=f'http://127.0.0.1:{port}'
        with httpx.Client(trust_env=False,timeout=3,headers={'Authorization':'Bearer synthetic-contract'}) as client:
            contract=client.get(search_url+'/v1/catalog/contract').json()
            # BE fetch failure leaves active generation untouched.
            pointer=(runtime/'data/CURRENT').read_text();mode['fail']=True
            try:sync(runtime,fetch,embed=embed,fingerprint=lambda *_:'f'*64,apply=True)
            except RuntimeError:pass
            else:raise AssertionError('BE failure accepted')
            assert (runtime/'data/CURRENT').read_text()==pointer;mode['fail']=False
            # Slow export runs on another worker, never in Search's request loop.
            mode['slow']=True;errors=[]
            def slow_sync():
                try:sync(runtime,fetch,embed=embed,fingerprint=lambda *_:'f'*64,apply=True)
                except Exception as exc:errors.append(type(exc).__name__)
            worker=threading.Thread(target=slow_sync);worker.start();assert blocked.wait(2)
            before=time.monotonic()
            response=client.post(search_url+'/v1/search',json={'query':'','snapshotId':contract['snapshotId']})
            elapsed=time.monotonic()-before
            assert response.status_code==200 and elapsed<2
            release.set();worker.join(10);assert not worker.is_alive() and not errors;mode['slow']=False
            result['sync']={'syntheticContractHTTP':True,'initialProducts':first['products'],
                'failedFetchRetainsPointer':True,'searchDuringBlockedSyncStatus':200,'searchDuringBlockedSyncSeconds':elapsed}
        subprocess.run([str(PG/'pg_ctl'),'-D',str(cluster),'-l',str(OUT/'postgres.log'),'-o',f"-k {sockdir} -c listen_addresses=''",'-w','start'],check=True,stdout=subprocess.DEVNULL);started=True
        os.environ.update(PROFILING_DATABASE_URL=f'postgresql+psycopg://fixture_owner@/emmet4930_shared_fresh?host={sockdir}',
            PROFILING_SERVICE_TOKEN='synthetic-contract',PROFILING_CATALOG_SOURCE='search_http',
            PROFILING_SEARCH_BASE_URL=search_url,PROFILING_BACKEND_BASE_URL=base,
            PROFILING_TAGGER='none',PROFILING_STRICT_STARTUP='true')
        from profiling.settings import get_settings
        get_settings.cache_clear()
        from integration.profiler_free_runtime import app as profiler
        with TestClient(profiler) as client:
            body={'recipientUserId':args.recipient_id,'sourceVersion':1,'dislikedCategories':[], 'giftPreference':'합성 취향', 'reviews':[]}
            path='/api/internal/v1/ai/profile/extract-and-pool';headers={'Authorization':'Bearer synthetic-contract'}
            response=client.post(path,json=body,headers=headers);assert response.status_code==202,response.text
            deadline=time.monotonic()+10;status=None
            while time.monotonic()<deadline:
                with psycopg.connect(dbname='emmet4930_shared_fresh',user='fixture_owner',host=str(sockdir)) as db:
                    row=db.execute('SELECT status FROM ai_profile.profile_runs WHERE recipient_user_id=%s AND source_version=1',(args.recipient_id,)).fetchone()
                    status=row[0] if row else None
                if status in ('DELIVERED','FAILED'):break
                time.sleep(.02)
            assert status=='DELIVERED' and len(callbacks)==1,(status,callbacks)
            again=client.post(path,json=body,headers=headers);assert again.status_code in (200,202)
            time.sleep(.1);assert len(callbacks)==2 and callbacks[0]==callbacks[1]
            # Publication preserves the retained generation until explicit drain/rebind.
            export['data']['products'][0]['viewCount']+=1
            sync(runtime,fetch,embed=embed,fingerprint=lambda *_:'f'*64,apply=True)
            asyncio.run_coroutine_threadsafe(owner.refresh(),search_loop[0]).result(10)
            assert profiler.state.shared_catalog.current.snapshot==contract['snapshotId']
            refreshed=client.post('/api/internal/v1/ai/profile/catalog/refresh',json={
                'expectedSnapshotId':owner.current.snapshot_id,'drainTimeoutSeconds':5},headers=headers)
            assert refreshed.status_code==200 and refreshed.json()['changed'],refreshed.text
            after=client.post(path,json={**body,'sourceVersion':2},headers=headers)
            assert after.status_code==202,after.text
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                with psycopg.connect(dbname='emmet4930_shared_fresh',user='fixture_owner',host=str(sockdir)) as db:
                    row=db.execute('SELECT status,catalog_version_id FROM ai_profile.profile_runs WHERE recipient_user_id=%s AND source_version=2',(args.recipient_id,)).fetchone()
                if row and row[0]=='DELIVERED':break
                time.sleep(.02)
            assert row and row[0]=='DELIVERED' and str(row[1])==profiler.state.shared_catalog.check()
            assert len(callbacks)==3
            result['profiler']={'httpAccepted':202,'persisted':'DELIVERED','callbacks':3,
                'duplicateResendsSameStoredResult':True,'controlledRebind':200,'newWorkSavedWithNewGeneration':True,
                'modelEnabled':False,'searchOwnsEncoder':True}
        result.update(passed=True,synthetic=True,externalProviderCalls=0,realBEConnected=False,containersStarted=False)
    finally:
        release.set()
        if server:server.should_exit=True
        if thread:thread.join(10)
        backend.shutdown();backend.server_close()
        # The Search lifespan owns all generations, including retained snapshots.
        if started:subprocess.run([str(PG/'pg_ctl'),'-D',str(cluster),'-m','fast','-w','stop'],check=True,stdout=subprocess.DEVNULL)
        shutil.rmtree(sockdir)
        result['temporaryProcessesStopped']=True
        (OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
