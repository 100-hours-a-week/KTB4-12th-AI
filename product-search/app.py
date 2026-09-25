"""One search runtime shared by the internal HTTP API and the team QA UI."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Annotated, Literal
import shutil
import sqlite3
import time
import uuid
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from search import SearchService, SearchRequest, open_search
from search.embedding import Encoder, SharedEmbedding, SearchBusy
from search.identity import BIGINT_MAX
from search.models import ProductRequest, FeedbackRequest, ServiceSearchRequest, SnapshotMismatch
from search.responses import (
    APIErrorResponse, FeedbackResponse, HistoricalQASearchResponse,
    MetadataResponse, ProductsResponse, QASearchResponse, ReadyResponse, SearchResponse,
)

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
        columns = {row[1]: row[2] for row in con.execute('PRAGMA table_info(feedback)')}
        if columns and columns.get('product_id') != 'INTEGER':
            raise RuntimeError('Unsupported legacy QA database: stop the server and move runtime/qa.sqlite3 aside before starting with a new QA database')
        con.execute('CREATE TABLE IF NOT EXISTS searches(id TEXT PRIMARY KEY, created_at TEXT, request TEXT, response TEXT)')
        con.execute('CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY, search_id TEXT REFERENCES searches(id) ON DELETE CASCADE, created_at TEXT, product_id INTEGER, verdict TEXT, note TEXT)')


def record(result):
    id = uuid.uuid4().hex
    result['searchId'] = id
    with db() as con:
        con.execute('INSERT INTO searches VALUES(?,?,?,?)', (id, datetime.now(timezone.utc).isoformat(),
                    json.dumps(result['request'], ensure_ascii=False), json.dumps(result, ensure_ascii=False)))
        con.execute('''DELETE FROM searches
            WHERE id IN (SELECT id FROM searches ORDER BY created_at DESC LIMIT -1 OFFSET 5000)
            AND NOT EXISTS (SELECT 1 FROM feedback WHERE feedback.search_id=searches.id)''')
    return result


def build_assets():
    files = ['app.js','app.css','guide.js','guide.css','json.js']
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
    app.state.active_profile_searches = 0
    async with open_search(ROOT) as service:
        app.state.search = service
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False


app = FastAPI(title="상품 검색 API", version="0.1.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=700, compresslevel=4)


class APIException(HTTPException):
    def __init__(self, status_code, code, message, *, headers=None):
        self.code = code
        super().__init__(status_code, message, headers=headers)


def error_responses(*statuses):
    return {status: {'model': APIErrorResponse} for status in statuses}


def error_response(status, code, message, *, issues=None, headers=None):
    body = APIErrorResponse.model_validate({'message': message, 'error': {'code': code, 'issues': issues}})
    return JSONResponse(body.model_dump(exclude_none=True), status_code=status, headers=headers)


@app.middleware('http')
async def headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    if request.url.path.startswith(('/api/', '/v1/')) or request.url.path in ('/healthz', '/readyz'):
        response.headers.setdefault('Cache-Control','no-store')
    return response


@app.exception_handler(RequestValidationError)
async def invalid(request, exc):
    issues = [{"field":'.'.join(str(s) for s in x['loc'][1:]),"message":x['msg']} for x in exc.errors()]
    return error_response(422, 'INVALID_REQUEST', '검색 조건을 확인해 주세요.', issues=issues)


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    code = getattr(exc, 'code', {404: 'NOT_FOUND', 503: 'SEARCH_NOT_READY'}.get(exc.status_code, 'INVALID_REQUEST'))
    return error_response(exc.status_code, code, str(exc.detail), headers=exc.headers)


@app.get('/', response_class=HTMLResponse)
async def index(request: Request):
    html=(ROOT/'static/index.html').read_text().replace('__ASSET_VERSION__',request.app.state.asset_version)
    return HTMLResponse(html, headers={'Cache-Control':'no-cache'})


@app.get('/guide', response_class=HTMLResponse, include_in_schema=False)
async def guide(request: Request):
    html = (ROOT/'static/guide.html').read_text().replace('__ASSET_VERSION__', request.app.state.asset_version)
    return HTMLResponse(html, headers={'Cache-Control': 'no-cache'})


@app.get('/healthz', response_model=ReadyResponse, responses=error_responses(503))
@app.get('/readyz', response_model=ReadyResponse, responses=error_responses(503))
async def health(request: Request):
    if not getattr(request.app.state, 'ready', False):
        raise APIException(503, 'SEARCH_NOT_READY', '검색 서버가 준비되지 않았습니다.')
    service=request.app.state.search
    return {'status':'ready','products':len(service.products),'snapshotId':service.snapshot_id,
            'catalogLoadError':getattr(service,'load_error',None),
            'exportGeneratedAt':service.catalog.get('export_generated_at')}


@app.get('/api/metadata', response_model=MetadataResponse)
@app.get('/v1/metadata', tags=['service'], response_model=MetadataResponse)
async def metadata(request: Request):
    return request.app.state.search.get_metadata()


async def execute_search(body: SearchRequest, request: Request, *, source: str, snapshot=None, save_qa=False):
    state = request.app.state
    if state.active_searches >= 12 or (source == 'profile' and state.active_profile_searches >= 4):
        raise APIException(503, 'SEARCH_BUSY', '검색 요청이 많습니다. 잠시 후 다시 검색해 주세요.', headers={'Retry-After':'1'})
    state.active_searches += 1
    if source == 'profile':
        state.active_profile_searches += 1
    try:
        result=await state.search.search(body, source=source, snapshot=snapshot)
        if save_qa:
            return await asyncio.to_thread(record, result)
        # Service calls do not persist or echo the user's original search request.
        return {key: value for key, value in result.items() if key != 'request'}
    except SnapshotMismatch as exc:
        raise APIException(409, 'SNAPSHOT_MISMATCH', str(exc)) from exc
    except ValueError as exc:
        raise APIException(422, getattr(exc, 'code', 'INVALID_REQUEST'), str(exc)) from exc
    except SearchBusy as exc:
        raise APIException(503, 'SEARCH_BUSY', '검색 요청이 많습니다. 잠시 후 다시 검색해 주세요.', headers={'Retry-After':'1'}) from exc
    except TimeoutError as exc:
        raise APIException(503, 'SEARCH_TIMEOUT', '검색 대기 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.', headers={'Retry-After':'1'}) from exc
    except RuntimeError as exc:
        logger.exception('Search execution failed')
        raise APIException(503, 'ENCODER_FAILURE', '검색 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.') from exc
    finally:
        state.active_searches -= 1
        if source == 'profile':
            state.active_profile_searches -= 1


@app.post('/api/search', tags=['qa'], response_model=QASearchResponse, responses=error_responses(422, 503))
async def search(body: SearchRequest, request: Request):
    return await execute_search(body, request, source='qa', save_qa=True)


@app.post('/v1/search', tags=['service'], response_model=SearchResponse, responses=error_responses(422, 409, 503))
async def service_search(
    body: ServiceSearchRequest,
    request: Request,
    source: Annotated[Literal['chat', 'profile'], Header(alias='X-Search-Source')] = 'profile',
):
    """Internal app search. Optional snapshotId pins a multi-call operation's catalog."""
    query = SearchRequest.model_validate(body.model_dump(exclude={'snapshot_id'}))
    return await execute_search(query, request, source=source, snapshot=body.snapshot_id)


@app.post('/api/products', response_model=ProductsResponse, responses=error_responses(422, 409))
@app.post('/v1/products', tags=['service'], response_model=ProductsResponse, responses=error_responses(422, 409))
async def products(body: ProductRequest, request: Request):
    try:
        return request.app.state.search.get_products(body.ids, body.snapshot_id)
    except SnapshotMismatch as exc:
        raise APIException(409, 'SNAPSHOT_MISMATCH', str(exc)) from exc


@app.get('/api/searches/{id}', response_model=HistoricalQASearchResponse,
         response_model_exclude_unset=True, responses=error_responses(404, 422))
def recorded_search(id: str):
    with db() as con:
        row=con.execute('SELECT response FROM searches WHERE id=?',(id,)).fetchone()
    if not row: raise APIException(404, 'NOT_FOUND', '검색 기록이 없거나 보관 기간이 지났습니다.')
    return json.loads(row[0])


@app.post('/api/feedback', response_model=FeedbackResponse, responses=error_responses(422, 404))
def feedback(body: FeedbackRequest):
    with db() as con:
        row=con.execute('SELECT response FROM searches WHERE id=?',(body.search_id,)).fetchone()
        if not row: raise APIException(404, 'NOT_FOUND', '평가할 검색 기록이 없습니다. 다시 검색해 주세요.')
        result=json.loads(row[0])
        if body.verdict!='missing' and body.product_id not in {p['productId'] for p in result['hits']}:
            raise APIException(422, 'INVALID_REQUEST', '이 검색 결과에 포함된 상품만 평가할 수 있습니다.')
        if body.verdict=='missing' and not body.note and not body.product_id:
            raise APIException(422, 'INVALID_REQUEST', '누락된 상품명이나 의견을 입력해 주세요.')
        id=uuid.uuid4().hex
        con.execute('INSERT INTO feedback VALUES(?,?,?,?,?,?)',(id,body.search_id,datetime.now(timezone.utc).isoformat(),body.product_id,body.verdict,body.note))
    return {'id':id,'saved':True}


@app.get('/images/{filename}', response_class=FileResponse, responses=error_responses(404, 422))
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


_generated_openapi = app.openapi


def contract_openapi():
    document = _generated_openapi()

    def preserve_integer_bounds(value):
        if isinstance(value, dict):
            # FastAPI's OpenAPI Schema model normalizes numeric bounds to float.
            # Restore the exact signed-BIGINT limit after that conversion so the
            # published JSON does not claim the rounded, invalid upper endpoint.
            if value.get('type') == 'integer' and value.get('format') == 'int64':
                if value.get('maximum') == float(BIGINT_MAX):
                    value['maximum'] = BIGINT_MAX
                if value.get('minimum') == 1:
                    value['minimum'] = 1
            for child in value.values():
                preserve_integer_bounds(child)
        elif isinstance(value, list):
            for child in value:
                preserve_integer_bounds(child)

    preserve_integer_bounds(document)
    return document


app.openapi = contract_openapi
