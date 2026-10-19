"""Explicit synthetic checks and owner-only metadata over in-memory transports."""
import asyncio
import json
import uuid

import httpx
import pytest

from console import server
from console.tests.auth_support import configured_app, fixture, login


def plan():
    return {name: str(uuid.uuid4()) for name in ('request_id', 'conversation_id', 'turn_id')} | {
        'senderUserId': 7, 'recipientUserId': 42, 'confirmCost': True}


def config():
    return {'liveModel': True, 'budgetMode': 'provider-key-limits', 'searchSnapshotId': 'synthetic-catalog',
        'targets': {'chat': 'http://127.0.0.1:1', 'backend': 'http://127.0.0.1:2', 'search': 'http://127.0.0.1:3'}}


def ready():
    return {'ready': True, 'database': 'postgresql', 'mode': 'live',
        'model': 'synthetic/model', 'budgetMode': 'provider-key-limits'}


@pytest.mark.parametrize('lost_ack', [False, True])
@pytest.mark.parametrize('restart', [False, True])
def test_quick_check_is_explicit_synthetic_and_retries_only_read_known_turns(tmp_path, lost_ack, restart):
    async def case():
        calls, writes = [], []
        stage = {'recovering': False}
        private = 'SYNTHETIC_PRIVATE_FIXTURE_CANARY'
        def upstream(request):
            calls.append((request.method, request.url.path))
            if stage['recovering']:
                assert request.method == 'GET' and '/turns/' in request.url.path
                return httpx.Response(200, json={'data': {'status': 'COMPLETED'}})
            if request.url.path == '/readyz':
                return httpx.Response(200, json=ready() if request.url.port == 1 else {
                    'status': 'ready', 'products': 3, 'snapshotId': 'synthetic-catalog'})
            if request.url.path.startswith('/fixture/'):
                return httpx.Response(200, json={'synthetic': True, 'recipientContext': {
                    'recipientUserId': 42, 'giftPreference': private}, 'preparedContext': {'productIds': [900]}})
            assert request.method == 'PUT'
            writes.append(json.loads(request.content))
            if '/turns/' in request.url.path:
                assert request.headers['X-Console-Quick-Check'] == 'true'
                if lost_ack: raise httpx.ReadError('synthetic lost acknowledgement')
                return httpx.Response(202, json={'data': {'status': 'PROCESSING'}})
            return httpx.Response(201, json={'data': {}})
        password_file, _ = fixture()
        settings = config() | {'adminAuth': {'passwordHashFile': str(password_file), 'sessionFile': str(tmp_path / 'session.sqlite3')}}
        app = server.create_app(settings)
        await app.state.backend.http.aclose()
        app.state.backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1:9000',
            headers={'Origin': 'http://127.0.0.1:9000'}) as client:
            body = plan()
            assert (await client.post('/api/chat/quick-check', json=body)).status_code == 401
            await login(client)
            assert (await client.post('/api/chat/quick-check', json=body | {'confirmCost': False})).status_code == 400
            for bad in ({'confirmCost': 'true'}, {'message': private}, {'recipientContext': {}}):
                assert (await client.post('/api/chat/quick-check', json=body | bad)).status_code == 422
            assert calls == []
            response = await client.post('/api/chat/quick-check', json=body)
            assert response.status_code == (502 if lost_ack else 200)
            assert len(writes) == 2
            assert writes[-1] == {'message': server.QUICK_CHECK_MESSAGE, 'quickCheck': True, 'recipientContext': {
                'recipientUserId': 42, 'status': 'NONE', 'sourceVersion': 0,
                'giftPreference': None, 'dislikedCategories': []}}
            assert private not in json.dumps(writes)
            stage['recovering'] = True
            if restart:
                await app.state.backend.http.aclose()
                await app.state.provider_usage.close()
                app = server.create_app(settings)
                await app.state.backend.http.aclose()
                app.state.backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
                # Same cookie and CSRF, new Console process, same isolated store.
                client._transport = httpx.ASGITransport(app)
            previous = len(calls)
            recovered = await client.post('/api/chat/quick-check', json=body)
            assert recovered.status_code == 200 and recovered.json()['httpStatus'] == 200
            assert recovered.json()['plan'] == {key: value for key, value in body.items() if key != 'confirmCost'} | {'message': server.QUICK_CHECK_MESSAGE}
            assert len(calls) == previous + 1 and len(writes) == 2
            assert (await client.post('/api/chat/quick-check', json=body | {'senderUserId': 8})).status_code == 409
            # A new check cannot inherit even this session's previous messages.
            other = plan() | {'conversation_id': body['conversation_id']}
            response = await client.post('/api/chat/quick-check', json=other)
            assert response.status_code == 409 and response.json()['detail'] == 'QUICK_CHECK_NEW_CONVERSATION_REQUIRED'
        await app.state.backend.http.aclose()
        await app.state.provider_usage.close()
    asyncio.run(case())


@pytest.mark.parametrize('legacy', [False, True])
def test_missing_upstream_turn_retry_keeps_grant_guard_and_reports_prerequisite(legacy):
    async def case():
        stage = {'retry': False}
        sent = []
        def upstream(request):
            if request.url.path == '/readyz':
                return httpx.Response(200, json=ready() if request.url.port == 1 else {
                    'status': 'ready', 'products': 3, 'snapshotId': 'synthetic-catalog'})
            if request.url.path.startswith('/fixture/'):
                return httpx.Response(200, json={'synthetic': True, 'recipientContext': {'recipientUserId': 42}})
            if '/turns/' not in request.url.path:
                return httpx.Response(200 if stage['retry'] else 201, json={'data': {}})
            if request.method == 'GET': return httpx.Response(404, json={'message': 'absent'})
            assert request.headers['X-Console-Quick-Check'] == 'true'
            assert json.loads(request.content)['quickCheck'] is True
            sent.append(json.loads(request.content))
            if stage['retry'] and legacy:
                return httpx.Response(400, json={'message': '요청 형식이나 값이 올바르지 않습니다.', 'error': {'code': 'INVALID_REQUEST'}})
            return httpx.Response(409, json={'message': 'QUICK_CHECK_GRANT_REQUIRED', 'error': {'code': 'CONFLICT'}}) if stage['retry'] else httpx.Response(202, json={'data': {'status': 'PROCESSING'}})
        app = configured_app(config())
        await app.state.backend.http.aclose()
        app.state.backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1:9000',
            headers={'Origin': 'http://127.0.0.1:9000'}) as client:
            await login(client)
            body = plan()
            assert (await client.post('/api/chat/quick-check', json=body)).status_code == 200
            stage['retry'] = True
            retry = await client.post('/api/chat/quick-check', json=body)
            assert retry.status_code == 409 and retry.json()['detail'] == ('QUICK_CHECK_REQUEST_UNSUPPORTED' if legacy else 'QUICK_CHECK_GRANT_REQUIRED')
            assert len(sent) == 2 and sent[0] == sent[1]
        await app.state.backend.http.aclose()
        await app.state.provider_usage.close()
    asyncio.run(case())


@pytest.mark.parametrize('fault,code', [
    ('not-live', 'QUICK_CHECK_LIVE_RUNTIME_REQUIRED'),
    ('no-fixture', 'QUICK_CHECK_SYNTHETIC_RECIPIENT_REQUIRED'),
    ('real-recipient', 'QUICK_CHECK_SYNTHETIC_RECIPIENT_REQUIRED'),
    ('wrong-recipient', 'QUICK_CHECK_SYNTHETIC_RECIPIENT_REQUIRED'),
    ('malformed-fixture', 'QUICK_CHECK_SYNTHETIC_RECIPIENT_REQUIRED'),
    ('offline-fixture', 'QUICK_CHECK_SYNTHETIC_RECIPIENT_REQUIRED'),
    ('empty-search', 'QUICK_CHECK_SEARCH_NOT_READY'),
    ('offline-search', 'QUICK_CHECK_SEARCH_NOT_READY'),
    ('changed-snapshot', 'QUICK_CHECK_SEARCH_NOT_READY'),
    ('guarded-budget', 'Actual live runtime not verified'),
])
def test_quick_check_prerequisites_fail_before_any_upstream_write(fault, code):
    async def case():
        settings = config()
        if fault == 'not-live': settings['liveModel'] = False
        if fault == 'no-fixture': settings['targets'].pop('backend')
        if fault == 'guarded-budget': settings['budgetMode'] = 'guarded'
        calls = []
        def upstream(request):
            calls.append(request.method)
            assert request.method == 'GET'
            if request.url.port == 1: return httpx.Response(200, json=ready())
            if request.url.port == 2:
                if fault == 'offline-fixture': raise httpx.ConnectError('synthetic unavailable fixture')
                return httpx.Response(200, json=[] if fault == 'malformed-fixture' else {
                    'synthetic': fault != 'real-recipient',
                    'recipientContext': {'recipientUserId': 99 if fault == 'wrong-recipient' else 42}})
            if fault == 'offline-search': raise httpx.ConnectError('synthetic unavailable search')
            return httpx.Response(200, json={'status': 'ready', 'products': 0 if fault == 'empty-search' else 3,
                'snapshotId': 'different' if fault == 'changed-snapshot' else 'synthetic-catalog'})
        app = configured_app(settings)
        await app.state.backend.http.aclose()
        app.state.backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1:9000',
            headers={'Origin': 'http://127.0.0.1:9000'}) as client:
            await login(client)
            response = await client.post('/api/chat/quick-check', json=plan())
            assert response.status_code == 409 and response.json()['detail'] == code
            assert all(method == 'GET' for method in calls)
        await app.state.backend.http.aclose()
        await app.state.provider_usage.close()
    asyncio.run(case())


def test_diagnostics_require_current_session_ownership_and_remove_report_prose():
    async def case():
        calls = []
        body = plan()
        private = 'SYNTHETIC_PRIVATE_REPORT_CANARY'
        def upstream(request):
            calls.append(request.url.path)
            if request.url.path.endswith('/report'):
                return httpx.Response(200, json={'conversationId': body['conversation_id'], 'turnId': body['turn_id'],
                    'message': private, 'blocks': [{'text': private}], 'diagnostics': {'raw': private},
                    'status': 'COMPLETED', 'metrics': {'elapsedMs': 21, 'raw': private},
                    'processing': {'profile': {'status': 'AVAILABLE', 'sourceVersion': 8,
                        'analysisSourceVersion': 7, 'receivedAt': '2026-10-19T12:00:00Z', 'giftPreference': private},
                        'retrieval': {'candidateCount': 4, 'catalogSnapshotId': 'synthetic-catalog',
                            'exclusionReasons': [{'code': private, 'count': 7}]},
                        'model': {'name': 'synthetic/model', 'status': 'completed', 'reasoning': private}}})
            if request.url.path == '/readyz': return httpx.Response(200, json=ready())
            return httpx.Response(202 if '/turns/' in request.url.path else 201, json={'data': {'status': 'PROCESSING'}})
        settings = config(); settings['targets'].pop('backend')
        app = configured_app(settings)
        await app.state.backend.http.aclose()
        app.state.backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1:9000',
            headers={'Origin': 'http://127.0.0.1:9000'}) as owner, httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url='http://127.0.0.1:9000',
            headers={'Origin': 'http://127.0.0.1:9000'}) as other:
            await login(owner); await login(other)
            payload = {key: value for key, value in body.items() if key != 'confirmCost'} | {'message': private}
            assert (await owner.post('/api/chat', json=payload)).status_code == 200
            url = '/api/chat/' + body['turn_id'] + '/diagnostics'
            previous = len(calls)
            assert (await other.get(url)).status_code == 404
            assert len(calls) == previous
            response = await owner.get(url)
            assert response.status_code == 200 and private not in response.text
            result = response.json()
            assert result['final'] == {'status': 'COMPLETED', 'requestId': body['request_id'], 'elapsedMs': 21}
            assert result['retrieval']['candidateCount'] == 4 and result['retrieval']['exclusionReasons'] == []
            assert result['profile']['sourceVersion'] == 8 and result['profile']['analysisSourceVersion'] == 7
            assert result['transport']['exchanges'] == []
        await app.state.backend.http.aclose()
        await app.state.provider_usage.close()
    asyncio.run(case())
