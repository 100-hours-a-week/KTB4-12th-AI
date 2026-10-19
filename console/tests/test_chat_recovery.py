"""Recover submitted turns without reopening generation or spending gates."""
import asyncio
import json
import uuid

import httpx
import pytest
from fastapi import HTTPException

from console.server import Backend, ChatInput
from console.server import create_app
from console.tests.auth_support import fixture, login
from console.tests.test_persistent_sessions import temporary_store


def request_body():
    return ChatInput(**{name: uuid.uuid4() for name in
        ("request_id", "conversation_id", "turn_id")}, message="SYNTHETIC")


def readiness(opened):
    return {"ready": True, "mode": "live", "database": "postgresql",
            "model": "synthetic/test", "budgetMode": "guarded",
            "budgetAccount": "gift-chat-harness-2026-10-06",
            "sharedBudget": {"limitUsd": 5, "remainingUsd": 1 if opened else 0, "halted": None}}


@pytest.mark.parametrize("state", ["PROCESSING", "COMPLETED", "CANCELLED", "FAILED"])
@pytest.mark.parametrize("lost_ack", [False, True])
def test_known_or_uncertain_turn_reads_without_new_work_gate(state, lost_ack):
    async def case():
        calls = []
        gate_open = True
        def upstream(request):
            calls.append((request.method, request.url.path))
            if request.url.path == "/readyz":
                return httpx.Response(200, json=readiness(gate_open))
            if "/turns/" not in request.url.path:
                return httpx.Response(201, json={"data": {}})
            if request.method == "PUT":
                if lost_ack:
                    raise httpx.ReadError("synthetic lost acknowledgement")
                return httpx.Response(202, json={"data": {"status": "PROCESSING"}})
            return httpx.Response(200, json={"data": {"status": state}})
        backend = Backend({"liveModel": True, "budgetMode": "guarded", "targets": {"chat": "http://127.0.0.1:1"}})
        await backend.http.aclose()
        backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        body = request_body()
        try:
            if lost_ack:
                with pytest.raises(httpx.ReadError):
                    await backend.chat(body)
            else:
                await backend.chat(body)
            gate_open = False
            before = len(calls)
            reply = await backend.chat(body)
            assert reply["httpStatus"] == (202 if state == "PROCESSING" else 200)
            assert reply["body"]["data"]["status"] == state
            assert calls[before:] == [("GET", backend.chat_base() + str(body.conversation_id) + "/turns/" + str(body.turn_id))]
            assert sum(method == "PUT" and "/turns/" in path for method, path in calls) == 1
            before = len(calls)
            with pytest.raises(HTTPException) as error:
                await backend.chat(body.model_copy(update={"message": "changed"}))
            assert error.value.status_code == 409 and len(calls) == before
        finally:
            await backend.http.aclose()
    asyncio.run(case())


@pytest.mark.parametrize("read_fault", [404, 401, 503, "network", "invalid"])
def test_failed_read_never_assumes_absence_or_bypasses_new_work_gate(read_fault):
    async def case():
        calls = []
        gate_open = True
        def upstream(request):
            calls.append((request.method, request.url.path))
            if request.url.path == "/readyz":
                return httpx.Response(200, json=readiness(gate_open))
            if "/turns/" not in request.url.path:
                return httpx.Response(201, json={"data": {}})
            if request.method == "PUT":
                return httpx.Response(202, json={"data": {"status": "PROCESSING"}})
            if read_fault == "network":
                raise httpx.ReadError("synthetic state unavailable")
            if read_fault == "invalid":
                return httpx.Response(200, json={"data": {"status": "UNKNOWN"}})
            return httpx.Response(read_fault, json={"message": "synthetic lookup failure"})
        backend = Backend({"liveModel": True, "budgetMode": "guarded", "targets": {"chat": "http://127.0.0.1:1"}})
        await backend.http.aclose()
        backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        try:
            body = request_body()
            await backend.chat(body)
            gate_open = False
            before = len(calls)
            expected = httpx.ReadError if read_fault == "network" else HTTPException
            with pytest.raises(expected) as error:
                await backend.chat(body)
            if read_fault != "network":
                assert error.value.status_code == {404: 409, 401: 401, 503: 503, "invalid": 502}[read_fault]
            after = calls[before:]
            assert len(after) == (2 if read_fault == 404 else 1)
            assert all(method == "GET" for method, _ in after)
            assert (after[-1][1] == "/readyz") is (read_fault == 404)
            assert backend.admission(str(body.turn_id))["readable"]
        finally:
            await backend.http.aclose()
    asyncio.run(case())


@pytest.mark.parametrize('lost_ack', [False, True])
@pytest.mark.parametrize('stored_status', ['PROCESSING', 'COMPLETED'])
def test_http_restart_retains_owner_without_reopening_generation(tmp_path, lost_ack, stored_status):
    async def case():
        original, _ = fixture()
        password_file = tmp_path / 'admin.hash'
        password_file.write_bytes(original.read_bytes()); password_file.chmod(0o600)
        config = {'adminAuth': {'passwordHashFile': str(password_file)}, 'liveModel': True,
                  'budgetMode': 'provider-key-limits', 'targets': {'chat': 'http://127.0.0.1:1'}}
        calls, conversations, turns = [], set(), {}
        private_turn = str(uuid.uuid4())
        ready = True

        def upstream(request):
            calls.append((request.method, request.url.path))
            if request.url.path == '/readyz':
                return httpx.Response(200, json={'ready': ready, 'mode': 'live', 'database': 'postgresql',
                    'model': 'synthetic/test', 'budgetMode': 'provider-key-limits'})
            if '/turns/' not in request.url.path:
                existed = request.url.path in conversations
                conversations.add(request.url.path)
                return httpx.Response(200 if existed else 201, json={'data': {'turns': [
                    {'turnId': private_turn, 'status': 'COMPLETED', 'lastEventId': 23}]}} if existed else {'data': {}})
            base = request.url.path.split('/events')[0].split('/cancellation')[0]
            if request.url.path.endswith('/events'):
                return httpx.Response(200, text='id: 1\nevent: progress\ndata: {"message":"SYNTHETIC saved event"}\n\n',
                                      headers={'content-type': 'text/event-stream'})
            if request.url.path.endswith('/cancellation'):
                if turns[base] == 'PROCESSING': turns[base] = 'CANCELLED'
                return httpx.Response(200, json={'data': {'status': turns[base]}})
            if request.method == 'GET':
                return httpx.Response(200, json={'data': {'status': turns[base]}})
            assert request.method == 'PUT'
            turns[base] = stored_status
            if lost_ack: raise httpx.ReadError('SYNTHETIC lost turn acknowledgement')
            return httpx.Response(202, json={'data': {'status': stored_status}})

        async def start(overrides=None):
            app = create_app(config | (overrides or {}))
            await app.state.backend.http.aclose()
            app.state.backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
            return app

        def browser(app, cookies=None, csrf=None):
            return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1:9000',
                cookies=cookies, headers={'Origin': 'http://127.0.0.1:9000', **({'X-CSRF-Token': csrf} if csrf else {})})

        apps = []
        try:
            first = await start(); apps.append(first)
            body = request_body().model_dump(mode='json')
            async with browser(first) as client:
                await login(client)
                cookie, csrf = client.cookies.get('console_admin'), client.headers['X-CSRF-Token']
                accepted = await client.post('/api/chat', json=body)
                assert accepted.status_code == (502 if lost_ack else 200)
            await first.state.backend.http.aclose()
            ready = False
            second = await start(); apps.append(second)
            before = len(calls)
            async with browser(second, {'console_admin': cookie}, csrf) as client:
                assert (await client.get('/api/admin/session')).status_code == 200
                assert (await client.get('/api/chat/'+body['turn_id'])).json()['data']['status'] == stored_status
                assert 'SYNTHETIC saved event' in (await client.get('/api/chat/'+body['turn_id']+'/events')).text
                retried = await client.post('/api/chat', json=body)
                assert retried.json()['httpStatus'] == (202 if stored_status == 'PROCESSING' else 200)
                assert all(method == 'GET' and path != '/readyz' for method, path in calls[before:])
                assert (await client.put('/api/chat/'+body['turn_id']+'/cancellation', json={})).status_code == 200
                assert sum(method == 'PUT' and '/turns/' in path and not path.endswith('/cancellation') for method, path in calls) == 1
                unknown = str(uuid.uuid4()); before = len(calls)
                assert (await client.get('/api/chat/'+unknown)).status_code == 404
                assert (await client.put('/api/chat/'+unknown+'/cancellation', json={})).status_code == 404
                assert len(calls) == before

                ready = True
                async with browser(second) as other:
                    await login(other)
                    assert (await other.get('/api/chat/'+body['turn_id'])).status_code == 404
                    for field in ('request_id', 'conversation_id', 'turn_id'):
                        attempted = request_body().model_dump(mode='json'); attempted[field] = body[field]
                        before = len(calls)
                        assert (await other.post('/api/chat', json=attempted)).status_code == 409
                        assert not any(method != 'GET' or '/turns/' in path for method, path in calls[before:])

                continued = request_body().model_dump(mode='json')
                continued['conversation_id'] = body['conversation_id']
                assert (await client.post('/api/chat', json=continued)).status_code == (502 if lost_ack else 200)
                assert (await client.post('/api/chat', json=continued)).json()['body']['data']['status'] == stored_status
                assert sum(method == 'PUT' and '/turns/' in path and not path.endswith('/cancellation') for method, path in calls) == 2

                for overrides in ({'targets': {'chat': 'http://127.0.0.1:2'}}, {'liveModel': False},
                                  {'budgetMode': 'guarded'}, {'database': 'postgresql://synthetic/other'}):
                    changed = await start(overrides); apps.append(changed)
                    before = len(calls)
                    async with browser(changed, {'console_admin': cookie}, csrf) as moved:
                        assert (await moved.get('/api/chat/'+body['turn_id'])).status_code == 404
                        assert (await moved.post('/api/chat', json=body)).status_code == 409
                    assert len(calls) == before

                # Purging a login does not permit a new login to claim its old AI conversation.
                assert (await client.post('/api/admin/logout')).status_code == 200
                await login(client)
                before = len(calls)
                rejected = await client.post('/api/chat', json=body)
                assert rejected.status_code == 409 and private_turn not in rejected.text
                assert rejected.json()['transport']['exchanges'][-1]['responseBody'] is None
                assert not any('/turns/' in path for _, path in calls[before:])
        finally:
            for app in apps:
                await app.state.backend.http.aclose()
                await app.state.provider_usage.close()
    asyncio.run(case())


@pytest.mark.parametrize('failed_write', [1, 2])
def test_failed_durable_write_stops_before_turn_send(tmp_path, monkeypatch, failed_write):
    async def case():
        original, _ = fixture()
        path = tmp_path / 'admin.hash'; path.write_bytes(original.read_bytes()); path.chmod(0o600)
        app = create_app({'adminAuth': {'passwordHashFile': str(path)}, 'targets': {'chat': 'http://127.0.0.1:1'}})
        calls = []
        def upstream(request):
            calls.append((request.method, request.url.path))
            return httpx.Response(200 if request.method == 'GET' else 201, json={'mode': 'mock', 'data': {}})
        await app.state.backend.http.aclose(); app.state.backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        original_save = app.state.admin.save_chat_admission
        writes = 0
        def failed(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == failed_write: raise HTTPException(503, 'SYNTHETIC_STORE_UNAVAILABLE')
            return original_save(*args, **kwargs)
        monkeypatch.setattr(app.state.admin, 'save_chat_admission', failed)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1:9000',
                                         headers={'Origin': 'http://127.0.0.1:9000'}) as client:
                await login(client)
                response = await client.post('/api/chat', json=request_body().model_dump(mode='json'))
                assert response.status_code == 503
                assert not any('/turns/' in url for _, url in calls)
                assert sum(method == 'PUT' for method, _ in calls) == failed_write-1
        finally:
            await app.state.backend.http.aclose(); await app.state.provider_usage.close()
    asyncio.run(case())


def test_restart_retries_absent_turn_with_original_frozen_context(tmp_path):
    async def case():
        auth = temporary_store(tmp_path)
        config = {'liveModel': True, 'budgetMode': 'provider-key-limits',
                  'targets': {'chat': 'http://127.0.0.1:1', 'backend': 'http://127.0.0.1:2'}}
        first = Backend(config, admission_store=auth)
        second = Backend(config, admission_store=auth)
        sent, calls = [], []
        version = 1
        async def request(service, method, path, body=None):
            calls.append((service, method, path))
            if path == '/readyz':
                return 200, {'ready': True, 'mode': 'live', 'database': 'postgresql',
                             'model': 'synthetic/test', 'budgetMode': 'provider-key-limits'}
            if service == 'backend':
                return 200, {'synthetic': True, 'recipientContext': {'syntheticVersion': version}}
            if method == 'GET': return 404, {}
            if '/turns/' not in path: return (201 if version == 1 else 200), {'data': {}}
            sent.append(json.loads(json.dumps(body)))
            return (503 if version == 1 else 202), {'data': {'status': 'PROCESSING'}}
        first.request = second.request = request
        body = request_body()
        try:
            assert (await first.chat(body, 'synthetic-owner-one'))['httpStatus'] == 503
            version = 2
            before = len(calls)
            assert (await second.chat(body, 'synthetic-owner-one'))['httpStatus'] == 202
            assert calls[before][1] == 'GET' and '/turns/' in calls[before][2]
            assert all(service != 'backend' for service, _, _ in calls[before:])
            assert sent[0] == sent[1] and sent[1]['recipientContext'] == {'syntheticVersion': 1}
        finally:
            await first.http.aclose(); await second.http.aclose()
    asyncio.run(case())


@pytest.mark.parametrize("fault", ["read", "timeout", "owned_save"])
def test_unconfirmed_conversation_can_restart_without_claiming_or_sending_a_turn(tmp_path, monkeypatch, fault):
    async def case():
        auth = temporary_store(tmp_path); owner = "synthetic-owner-one"
        config = {"targets": {"chat": "http://127.0.0.1:1"}}
        first, restarted = Backend(config, admission_store=auth), Backend(config, admission_store=auth)
        conversations, turns = set(), []
        fail = True
        async def request(service, method, path, body=None):
            if path == "/readyz": return 200, {"mode": "mock"}
            if "/turns/" in path:
                turns.append(path)
                return 202, {"data": {"status": "PROCESSING"}}
            existed = path in conversations
            conversations.add(path)
            if fail and fault in {"read", "timeout"}:
                raise (httpx.ReadError if fault == "read" else httpx.ReadTimeout)("SYNTHETIC lost creation reply")
            return (200 if existed else 201), {"data": {}}
        first.request = restarted.request = request
        save = auth.save_chat_admission
        def save_owned(*args):
            if fail and fault == "owned_save" and args[-1].get("conversationOwned"):
                raise HTTPException(503, "SYNTHETIC store unavailable")
            return save(*args)
        monkeypatch.setattr(auth, "save_chat_admission", save_owned)
        body = request_body()
        try:
            with pytest.raises(HTTPException) as failed: await first.chat(body, owner)
            assert failed.value.detail == ("SYNTHETIC store unavailable" if fault == "owned_save" else "CONSOLE_CONVERSATION_UNCONFIRMED")
            assert not turns
            saved = auth.chat_admission(owner, first.target_scope, request_id=str(body.request_id))
            assert not saved["conversationOwned"] and not saved["readable"]
            fail = False
            with pytest.raises(HTTPException, match="CONSOLE_CONVERSATION_NOT_OWNED"):
                await restarted.chat(body, owner)
            assert not turns
            fresh = request_body().model_copy(update={"message": body.message})
            assert (await restarted.chat(fresh, owner))["httpStatus"] == 202
            assert len(turns) == 1 and str(fresh.conversation_id) in turns[0]
        finally:
            await first.http.aclose(); await restarted.http.aclose()
    asyncio.run(case())
