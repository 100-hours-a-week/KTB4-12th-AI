import asyncio
import hashlib
import json
from pathlib import Path
import uuid

import httpx
import pytest

from fastapi import HTTPException

from console.admin_auth import AdminAuth, TTL, password_hash
from console.server import create_app
from console.tests.auth_support import fixture


@pytest.mark.parametrize('fault', ['logout', 'password_change', 'absolute_expiry', 'corrupt', 'permissions'])
def test_persistent_session_revocation_and_storage_failure(tmp_path, fault):
    async def case():
        original, password = fixture()
        path = tmp_path / 'admin.hash'; path.write_bytes(original.read_bytes()); path.chmod(0o600)
        config = {'adminAuth': {'passwordHashFile': str(path)}}
        now = [1800000000.0]
        def start():
            app = create_app(config); app.state.admin.clock = lambda: now[0]
            return app
        first = start()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(first), base_url='http://127.0.0.1:9000') as browser:
            logged = await browser.post('/api/admin/login', json={'password': password}, headers={'Origin': 'http://127.0.0.1:9000'})
            assert logged.status_code == 200
            cookie, csrf = browser.cookies.get('console_admin'), logged.json()['csrf']
        store = first.state.admin.session_file
        owner = hashlib.sha256(cookie.encode()).hexdigest()
        first.state.admin.save_chat_admission(owner, 'synthetic-target', admission())
        assert store.stat().st_mode & 0o777 == 0o600
        assert cookie.encode() not in store.read_bytes() and password.encode() not in store.read_bytes()
        await first.state.backend.http.aclose()
        second = start(); now[0] += 29*86400
        async with httpx.AsyncClient(transport=httpx.ASGITransport(second), base_url='http://127.0.0.1:9000', cookies={'console_admin': cookie}) as reopened:
            state = await reopened.get('/api/admin/session')
            assert state.status_code == 200 and state.json()['expiresIn'] == 86400 and state.json()['csrf'] == csrf
            assert (await reopened.post('/api/admin/logout', headers={'Origin': 'http://127.0.0.1:9000'})).status_code == 403
            if fault == 'logout':
                assert (await reopened.post('/api/admin/logout', headers={'Origin': 'http://127.0.0.1:9000', 'X-CSRF-Token': csrf})).status_code == 200
            elif fault == 'password_change': path.write_text(password_hash(password + 'new'))
            elif fault == 'absolute_expiry': now[0] += 86400
            elif fault == 'corrupt': store.write_bytes(b'synthetic broken database')
            else: store.chmod(0o644)
        await second.state.backend.http.aclose()
        third = start()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(third), base_url='http://127.0.0.1:9000', cookies={'console_admin': cookie}) as stale:
            r = await stale.get('/api/admin/session')
            assert r.status_code == (503 if fault in {'corrupt', 'permissions'} else 401)
            assert cookie not in r.text and password not in r.text
        if fault in {'logout', 'password_change', 'absolute_expiry'}:
            with third.state.admin.session_store() as db:
                assert db.execute('SELECT count(*) FROM chat_admissions').fetchone()[0] == 0
        await third.state.backend.http.aclose()
    asyncio.run(case())


def admission():
    body = {name: str(uuid.uuid4()) for name in ('request_id', 'conversation_id', 'turn_id')}
    body.update(message='SYNTHETIC durable request', senderUserId=7, recipientUserId=42,
                recipientContext={'synthetic': 'frozen before send'}, preparedContext=None)
    return {'body': body, 'fingerprint': hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
            'readable': False, 'conversationOwned': False, 'receipt': None}


def temporary_store(tmp_path):
    original, _ = fixture()
    path = tmp_path / 'admin.hash'; path.write_bytes(original.read_bytes()); path.chmod(0o600)
    auth = AdminAuth({'passwordHashFile': str(path)})
    auth.clock = lambda: 1800000000.0
    with auth.session_store() as db:
        for owner in ('synthetic-owner-one', 'synthetic-owner-two'):
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                       (owner, 'synthetic-csrf', hashlib.sha256(auth.credential().encode()).hexdigest(), auth.clock(), auth.clock()+TTL))
    return auth


def test_admission_restart_preserves_frozen_body_and_target(tmp_path):
    auth = temporary_store(tmp_path)
    row = admission(); owner = 'synthetic-owner-one'; target = 'synthetic-target'
    auth.save_chat_admission(owner, target, row)
    row.update(readable=True, conversationOwned=True)
    auth.save_chat_admission(owner, target, row)
    restarted = AdminAuth({'passwordHashFile': auth.hash_file}); restarted.clock = auth.clock
    assert restarted.chat_admission(owner, target, turn_id=row['body']['turn_id']) == row
    assert restarted.owns_chat_conversation(owner, target, row['body']['conversation_id'])
    with pytest.raises(HTTPException) as changed_target:
        restarted.chat_admission(owner, 'different-target-or-mode', request_id=row['body']['request_id'])
    assert changed_target.value.status_code == 409
    changed = {**row, 'body': {**row['body'], 'recipientContext': {'synthetic': 'later profile'}}}
    with pytest.raises(HTTPException) as changed_body:
        restarted.save_chat_admission(owner, target, changed)
    assert changed_body.value.status_code == 409
    assert restarted.chat_admission(owner, target, turn_id=row['body']['turn_id']) == row


@pytest.mark.parametrize('field', ['request_id', 'conversation_id', 'turn_id'])
def test_another_login_cannot_claim_known_identifiers(tmp_path, field):
    auth = temporary_store(tmp_path); first = admission()
    auth.save_chat_admission('synthetic-owner-one', 'synthetic-target', first)
    second = admission(); second['body'][field] = first['body'][field]
    with pytest.raises(HTTPException) as rejected:
        auth.save_chat_admission('synthetic-owner-two', 'synthetic-target', second)
    assert rejected.value.status_code == 409
    with pytest.raises(HTTPException) as unreadable:
        auth.chat_admission('synthetic-owner-two', 'synthetic-target', turn_id=first['body']['turn_id'])
    assert unreadable.value.status_code == 404


def test_unusable_admission_storage_fails_closed(tmp_path):
    auth = temporary_store(tmp_path)
    auth.session_file.chmod(0o644)
    with pytest.raises(HTTPException) as rejected:
        auth.save_chat_admission('synthetic-owner-one', 'synthetic-target', admission())
    assert rejected.value.status_code == 503
