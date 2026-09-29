import asyncio

import httpx
import pytest

from http_security import service_url
from search.http_limits import MAX_REQUEST_BYTES, RequestBodyLimit
from search_client import SearchClient
from tools.sync_catalog import fetcher


@pytest.mark.parametrize('url', [
    'http://backend.example/export', 'https://user:secret@backend.example/export',
    'https://backend.example/export?token=secret', 'https://backend.example/export#secret',
    'file:///etc/passwd', '//backend.example/export', 'https://backend.example/\nsecret',
])
def test_unsafe_destinations_fail_without_disclosing_input(url):
    with pytest.raises(ValueError) as error:
        service_url(url)
    assert 'secret' not in str(error.value) and url not in str(error.value)


@pytest.mark.parametrize('url', [
    'https://backend.example/export', 'http://127.0.0.1:8080/export',
    'http://[::1]:8080/export', 'http://localhost:8080/export',
])
def test_https_and_loopback_destinations(url):
    assert str(service_url(url)) == url


def test_internal_http_requires_explicit_opt_in():
    url = 'http://backend:8080/export'
    assert str(service_url(url, allow_insecure_http=True)) == url


@pytest.mark.parametrize('categories', [
    'https://other.example/categories', 'https://backend.example:8443/categories',
    'http://backend.example/categories',
])
def test_sync_rejects_different_origin_before_any_network_io(categories, monkeypatch):
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: pytest.fail('must not create client'))
    with pytest.raises(ValueError):
        fetcher('https://backend.example/export', categories, 'test-only', allow_insecure_http=True)


def test_sync_authenticates_same_origin_and_never_follows_redirects(monkeypatch):
    client = httpx.Client
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'path': request.url.path})

    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    fetch = fetcher('https://backend.example:443/export', 'https://backend.example/categories', 'test-only')
    assert fetch() == [{'path': '/export'}, {'path': '/categories'}]
    assert all(request.headers['authorization'] == 'Bearer test-only' for request in requests)
    requests.clear()

    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={'Location': 'https://other.example/secret'})

    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: client(transport=httpx.MockTransport(redirect), **kwargs))
    with pytest.raises(RuntimeError, match='HTTP 302'):
        fetch()
    assert len(requests) == 1


async def test_authenticated_client_rejects_plaintext_and_preserves_private_opt_in():
    with pytest.raises(ValueError, match='HTTPS'):
        SearchClient('http://backend:8000', token='test-only')
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={'Location': 'https://other.example'})

    async with SearchClient('http://backend:8000', token='test-only', allow_insecure_http=True,
                            transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.ready()
    assert len(requests) == 1


@pytest.mark.parametrize('token', ['', ' leading', 'trailing ', 'a\r\nb', 'a\x00b', '한글'])
def test_client_rejects_invalid_header_tokens(token):
    with pytest.raises(ValueError):
        SearchClient('https://backend.example', token=token)


@pytest.mark.parametrize('length', [None, b'1', str(MAX_REQUEST_BYTES + 1).encode(), b'9' * 5000])
async def test_oversized_stream_never_reaches_application(length):
    called = False

    async def app(scope, receive, send):
        nonlocal called
        called = True

    messages = iter([
        {'type': 'http.request', 'body': b'x' * MAX_REQUEST_BYTES, 'more_body': True},
        {'type': 'http.request', 'body': b'x', 'more_body': False},
    ])
    responses = []

    async def receive():
        return next(messages)

    async def send(message):
        responses.append(message)

    scope = {'type': 'http', 'path': '/api/feedback', 'headers': [] if length is None else [(b'content-length', length)]}
    await RequestBodyLimit(app)(scope, receive, send)
    assert not called and responses[0]['status'] == 413
    assert b'REQUEST_TOO_LARGE' in responses[1]['body']


async def test_body_limit_preserves_valid_stream_and_disconnect():
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    async def app(scope, receive, send):
        body = await Request(scope, receive).body()
        await JSONResponse({'size': len(body)})(scope, receive, send)

    async def chunks():
        yield b'x' * (MAX_REQUEST_BYTES // 2)
        yield b'x' * (MAX_REQUEST_BYTES // 2)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=RequestBodyLimit(app)), base_url='http://test') as client:
        response = await client.post('/v1/search', content=chunks())
    assert response.status_code == 200 and response.json() == {'size': MAX_REQUEST_BYTES}

    async def disconnected():
        return {'type': 'http.disconnect'}

    async def forbidden(*args):
        pytest.fail('disconnected request must not be processed')

    await asyncio.wait_for(RequestBodyLimit(forbidden)(
        {'type': 'http', 'path': '/api/search', 'headers': []}, disconnected, forbidden), timeout=1)
