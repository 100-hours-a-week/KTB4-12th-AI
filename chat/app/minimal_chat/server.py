"""Session-authenticated BE adapter over /v1/chat HTTP, with no chat-store access."""
import asyncio
import html
import json
import os
import secrets
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator

STATIC = Path(__file__).with_name('static')
TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED'}


class Config:
    def __init__(self, service_token, access_code, session_secret, origin='https://wjo-ai-chat-demo.hf.space', secure=True):
        if not all(isinstance(x, str) and x for x in (service_token, access_code, session_secret)):
            raise RuntimeError('Existing AI_SERVICE_TOKEN, DEMO_ACCESS_TOKEN and SESSION_SECRET are required')
        u = urlsplit(origin)
        if u.username or u.password or u.path not in ('', '/') or u.query or u.fragment or not u.hostname:
            raise ValueError('AI_CHAT_URL must be a server origin without credentials or paths')
        if u.scheme != 'https' and not (u.scheme == 'http' and u.hostname in {'127.0.0.1', 'localhost', '::1'}):
            raise ValueError('Use HTTPS, or loopback HTTP for local tests')
        self.service_token, self.access_code = service_token, access_code
        self.signer = URLSafeTimedSerializer(session_secret, salt='minimal-chat')
        self.origin, self.secure = origin.rstrip('/'), secure

    @classmethod
    def existing(cls):
        return cls(os.environ.get('AI_SERVICE_TOKEN'), os.environ.get('DEMO_ACCESS_TOKEN'),
                   os.environ.get('SESSION_SECRET'), os.getenv('AI_CHAT_URL', 'https://wjo-ai-chat-demo.hf.space'),
                   os.getenv('COOKIE_SECURE', 'true') == 'true')


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ConversationBody(Body):
    fixture: bool = False


class MessageBody(Body):
    message: str = Field(min_length=1, max_length=200)
    @field_validator('message', mode='before')
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value


class LoginBody(Body):
    token: str = Field(min_length=1, max_length=2048)


def create_app(config=None, upstream=None, existing_identity=None):
    """upstream may be an existing server-only HTTP client getter when mounted on HF."""
    sessions, attempts = {}, defaultdict(deque)
    config_cache = config
    def cfg():
        nonlocal config_cache
        if config_cache is None:
            config_cache = Config.existing()
        return config_cache

    @asynccontextmanager
    async def lifespan(app):
        if upstream is None:
            app.state.upstream = httpx.AsyncClient(base_url=cfg().origin,
                headers={'Authorization': 'Bearer ' + cfg().service_token}, follow_redirects=False,
                timeout=httpx.Timeout(30, connect=10))
        try:
            yield
        finally:
            if upstream is None:
                await app.state.upstream.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    def client():
        return upstream() if callable(upstream) else upstream if upstream is not None else app.state.upstream

    def error(status, message):
        return JSONResponse({'message': message}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return error(400, '요청 형식이나 값이 올바르지 않습니다.')

    @app.middleware('http')
    async def boundary(request, call_next):
        origin = request.headers.get('origin')
        relative_path = request.scope['path'].removeprefix(request.scope.get('root_path', ''))
        if relative_path.startswith('/api/') and ((origin and urlsplit(origin).netloc != request.headers.get('host'))
                or request.headers.get('sec-fetch-site') == 'cross-site'):
            return error(403, '같은 화면에서 다시 요청해 주세요.')
        # Bound actual received bytes, including requests without Content-Length.
        if request.method == 'PUT' or request.method == 'POST':
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 20000:
                    return error(413, '요청이 너무 큽니다.')
            request._body = bytes(data)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store, no-transform'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; base-uri 'none'; frame-ancestors 'self' https://huggingface.co"
        return response

    def session(request):
        try:
            sid = cfg().signer.loads(request.cookies.get('minimal_chat_session', ''), max_age=28800)
            entry = sessions.get(sid)
            if entry and entry['expires'] > time.time():
                return entry
        except BadSignature:
            pass
        return None

    def start_session(request, sender=None):
        for sid in list(sessions):
            if sessions[sid]['expires'] <= time.time():
                del sessions[sid]
        if len(sessions) >= 512:
            return error(429, '접속이 많습니다. 잠시 후 다시 시도해 주세요.')
        sid = secrets.token_urlsafe(24)
        sessions[sid] = {'sender': sender or secrets.randbelow(2**53 - 1) + 1, 'conversations': {}, 'expires': time.time() + 28800}
        response = JSONResponse({'ready': True})
        response.set_cookie('minimal_chat_session', cfg().signer.dumps(sid), httponly=True,
                            secure=cfg().secure, samesite='strict', max_age=28800,
                            path=(request.scope.get('root_path', '') or '') + '/')
        return response

    @app.get('/api/session')
    async def status(request: Request):
        if session(request):
            return {'ready': True}
        if existing_identity is not None:
            sender = await existing_identity(request)
            if sender:
                return start_session(request, sender)
        return error(401, '체험 코드를 입력해 주세요.')

    @app.post('/api/session')
    async def login(request: Request, body: LoginBody):
        key = request.client.host if request.client else 'local'
        q = attempts[key];now = time.time()
        while q and q[0] < now - 300:
            q.popleft()
        if len(q) >= 10:
            return error(429, '잠시 후 다시 접속해 주세요.')
        q.append(now)
        if len(attempts) > 5000:
            for candidate in list(attempts)[:1000]:
                if not attempts[candidate] or attempts[candidate][-1] < now - 300:
                    del attempts[candidate]
        if not secrets.compare_digest(body.token.encode(), cfg().access_code.encode()):
            return error(401, '체험 코드를 확인해 주세요.')
        return start_session(request)

    def owned(request, cid):
        entry = session(request)
        if entry is None:
            return None, error(401, '체험 코드로 다시 접속해 주세요.')
        binding = entry['conversations'].get(str(cid))
        if binding is None or not binding['verified']:
            return None, error(404, '이 접속에서 시작한 대화를 찾을 수 없습니다.')
        base = '/v1/chat/test/conversations/' if binding['fixture'] else '/v1/chat/conversations/'
        return base + str(cid), None

    async def relay(method, path, body=None):
        try:
            r = await client().request(method, path, **({'json': body} if body is not None else {}))
            if r.is_redirect:
                return error(503, 'AI 서버 주소를 확인해 주세요.')
            value = r.json()
            if not isinstance(value, dict):
                raise ValueError()
            return JSONResponse(value, status_code=r.status_code)
        except (httpx.HTTPError, ValueError):
            return error(503, 'AI 서버 연결을 확인한 뒤 다시 연결해 주세요.')

    @app.put('/api/conversations/{cid}')
    async def conversation(cid: UUID4, request: Request, body: ConversationBody):
        entry = session(request)
        if entry is None:
            return error(401, '체험 코드로 다시 접속해 주세요.')
        old = entry['conversations'].get(str(cid))
        if old is not None and old['fixture'] != body.fixture:
            return error(409, '대화의 실행 방식을 바꾸려면 새 대화를 시작해 주세요.')
        if old is None and len(entry['conversations']) >= 64:
            return error(429, '새 접속에서 다시 시작해 주세요.')
        binding = old or {'fixture': body.fixture, 'verified': False}
        entry['conversations'][str(cid)] = binding
        root = ('/v1/chat/test/conversations/' if body.fixture else '/v1/chat/conversations/') + str(cid)
        # Synthetic identities are only example scaffolding, not production member IDs.
        result = await relay('PUT', root, {'senderUserId': entry['sender'], 'recipientUserId': 1})
        # A rejected/uncertain create must never grant access to a guessed existing UUID.
        if result.status_code in (200, 201):
            value = json.loads(result.body)
            if value.get('data', {}).get('conversationId') != str(cid):
                return error(503, '대화 연결을 확인해 주세요.')
            binding['verified'] = True
        return result

    @app.get('/api/conversations/{cid}')
    async def read(cid: UUID4, request: Request):
        root, denied = owned(request, cid)
        return denied if denied is not None else await relay('GET', root)

    @app.put('/api/conversations/{cid}/turns/{tid}')
    async def turn(cid: UUID4, tid: UUID4, request: Request, body: MessageBody):
        root, denied = owned(request, cid)
        if denied is not None:
            return denied
        message = body.message.strip()
        if not message:
            return error(400, '질문을 입력해 주세요.')
        return await relay('PUT', root + '/turns/' + str(tid), {'message': message})

    @app.get('/api/conversations/{cid}/turns/{tid}')
    async def snapshot(cid: UUID4, tid: UUID4, request: Request):
        root, denied = owned(request, cid)
        return denied if denied is not None else await relay('GET', root + '/turns/' + str(tid))

    @app.put('/api/conversations/{cid}/turns/{tid}/cancellation')
    async def cancel(cid: UUID4, tid: UUID4, request: Request):
        root, denied = owned(request, cid)
        return denied if denied is not None else await relay('PUT', root + '/turns/' + str(tid) + '/cancellation')

    @app.get('/api/conversations/{cid}/turns/{tid}/events')
    async def events(cid: UUID4, tid: UUID4, request: Request, afterEventId: int = 0):
        root, denied = owned(request, cid)
        if denied is not None:
            return denied
        if afterEventId < 0 or set(request.query_params) - {'afterEventId'}:
            return error(400, '이벤트 번호를 확인해 주세요.')
        try:
            req = client().build_request('GET', root + '/turns/' + str(tid) + '/events',
                                        params={'afterEventId': afterEventId}, headers={'Accept': 'text/event-stream'})
            response = await client().send(req, stream=True)
        except httpx.HTTPError:
            return error(503, '답변을 다시 연결해 주세요.')
        if response.status_code != 200 or not response.headers.get('content-type', '').startswith('text/event-stream'):
            await response.aread();await response.aclose()
            return error(response.status_code if 400 <= response.status_code < 600 else 503, '답변을 다시 연결해 주세요.')
        async def chunks():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
        return StreamingResponse(chunks(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    async def page(request, fixture):
        prefix = html.escape(request.scope.get('root_path', ''), quote=True)
        source = (STATIC / 'index.html').read_text().replace('__PREFIX__', prefix).replace('__FIXTURE__', 'true' if fixture else 'false')
        return HTMLResponse(source.replace('__TITLE__', '고정 응답 채팅' if fixture else '채팅'))

    @app.get('/')
    async def index(request: Request):
        return await page(request, False)

    @app.get('/test')
    async def test(request: Request):
        return await page(request, True)
    return app
