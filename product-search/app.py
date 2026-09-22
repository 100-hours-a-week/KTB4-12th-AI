"""Thin HTTP adapter and QA UI. Chat/Profile import search.SearchService directly."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from search import SearchService, SearchRequest, open_search
from search.embedding import Encoder, SharedEmbedding, SearchBusy
from search.models import ProductRequest, FeedbackRequest

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
RUNTIME.mkdir(exist_ok=True)
DB = RUNTIME / "qa.sqlite3"
logger = logging.getLogger("product-search")


def db():
    con = sqlite3.connect(DB, timeout=5)
    con.execute('PRAGMA foreign_keys=ON')
    return con


def init_db():
    with db() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('CREATE TABLE IF NOT EXISTS searches(id TEXT PRIMARY KEY, created_at TEXT, request TEXT, response TEXT)')
        con.execute('CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY, search_id TEXT REFERENCES searches(id) ON DELETE CASCADE, created_at TEXT, product_id TEXT, verdict TEXT, note TEXT)')


def record(result):
    id = uuid.uuid4().hex
    result['searchId'] = id
    with db() as con:
        con.execute('INSERT INTO searches VALUES(?,?,?,?)', (id, datetime.now(timezone.utc).isoformat(),
                    json.dumps(result['request'], ensure_ascii=False), json.dumps(result, ensure_ascii=False)))
        con.execute('DELETE FROM searches WHERE id IN (SELECT id FROM searches ORDER BY created_at DESC LIMIT -1 OFFSET 5000)')
    return result


def build_assets():
    files = ['app.js','app.css']
    version = hashlib.sha256(b''.join((ROOT/'static'/x).read_bytes() for x in files)).hexdigest()[:16]
    target = ROOT/'static/assets'/version
    target.mkdir(parents=True, exist_ok=True)
    for name in files:
        if not (target/name).exists(): shutil.copy2(ROOT/'static'/name, target/name)
    return version


@asynccontextmanager
async def lifespan(app):
    init_db()
    app.state.asset_version = build_assets()
    app.state.active_searches = 0
    async with open_search(ROOT) as service:
        app.state.search = service
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False


app = FastAPI(title="선잘알 상품 검색", version="0.1.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=700, compresslevel=4)


@app.middleware('http')
async def headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    if request.url.path.startswith('/api/'):
        response.headers.setdefault('Cache-Control','no-store')
    return response


@app.exception_handler(RequestValidationError)
async def invalid(request, exc):
    issues = [{"field":'.'.join(str(s) for s in x['loc'][1:]),"message":x['msg']} for x in exc.errors()]
    return JSONResponse({'message':'검색 조건을 확인해 주세요.','issues':issues}, status_code=422)


@app.get('/', response_class=HTMLResponse)
async def index(request: Request):
    html=(ROOT/'static/index.html').read_text().replace('__ASSET_VERSION__',request.app.state.asset_version)
    return HTMLResponse(html, headers={'Cache-Control':'no-cache'})


@app.get('/healthz')
async def health(request: Request):
    service=request.app.state.search
    return {'status':'ready','products':len(service.products),'snapshotId':service.snapshot_id}


@app.get('/api/metadata')
async def metadata(request: Request):
    return request.app.state.search.get_metadata()


@app.post('/api/search')
async def search(body: SearchRequest, request: Request):
    if request.app.state.active_searches >= 12:
        raise HTTPException(503, '검색 요청이 많습니다. 잠시 후 다시 검색해 주세요.', headers={'Retry-After':'1'})
    request.app.state.active_searches += 1
    try:
        result=await request.app.state.search.search(body, source='qa')
        return await asyncio.to_thread(record, result)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (SearchBusy, TimeoutError) as exc:
        raise HTTPException(503, '검색 요청이 많습니다. 잠시 후 다시 검색해 주세요.') from exc
    except RuntimeError as exc:
        logger.exception('Search execution failed')
        raise HTTPException(503, '검색 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.') from exc
    finally:
        request.app.state.active_searches -= 1


@app.post('/api/products')
async def products(body: ProductRequest, request: Request):
    try:
        return request.app.state.search.get_products(body.ids, body.snapshot_id)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@app.get('/api/searches/{id}')
def recorded_search(id: str):
    with db() as con:
        row=con.execute('SELECT response FROM searches WHERE id=?',(id,)).fetchone()
    if not row: raise HTTPException(404,'검색 기록이 없거나 보관 기간이 지났습니다.')
    return json.loads(row[0])


@app.post('/api/feedback')
def feedback(body: FeedbackRequest):
    with db() as con:
        row=con.execute('SELECT response FROM searches WHERE id=?',(body.search_id,)).fetchone()
        if not row: raise HTTPException(404,'평가할 검색 기록이 없습니다. 다시 검색해 주세요.')
        result=json.loads(row[0])
        if body.verdict!='missing' and body.product_id not in {p['id'] for p in result['hits']}:
            raise HTTPException(422,'이 검색 결과에 포함된 상품만 평가할 수 있습니다.')
        if body.verdict=='missing' and not body.note and not body.product_id:
            raise HTTPException(422,'누락된 상품명이나 의견을 입력해 주세요.')
        id=uuid.uuid4().hex
        con.execute('INSERT INTO feedback VALUES(?,?,?,?,?,?)',(id,body.search_id,datetime.now(timezone.utc).isoformat(),body.product_id,body.verdict,body.note))
    return {'id':id,'saved':True}


@app.get('/images/{filename}')
async def image_file(filename: str):
    if not all(c.isalnum() or c in '-.' for c in filename) or '..' in filename:
        raise HTTPException(404)
    file=ROOT/'static/images'/filename
    if not file.is_file(): raise HTTPException(404)
    return FileResponse(file, headers={'Cache-Control':'public, max-age=31536000, immutable'},
                        media_type='image/avif' if filename.endswith('.avif') else 'image/webp')


class CachedAssets(StaticFiles):
    async def get_response(self,path,scope):
        response=await super().get_response(path,scope)
        response.headers['Cache-Control']='public, max-age=31536000, immutable'
        return response


(ROOT/'static/assets').mkdir(parents=True, exist_ok=True)
app.mount('/assets',CachedAssets(directory=ROOT/'static/assets'),name='assets')
