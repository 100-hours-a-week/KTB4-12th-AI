"""The real Console routes send only to in-memory synthetic BE/AI transports."""
import asyncio
import json
import uuid

import httpx
import pytest

from console import server
from console.tests.auth_support import configured_app, login


@pytest.mark.parametrize("settings,chat_model,profiler_model,chat_ok,profiler_ok", [
    ({}, "synthetic/chat-a", "synthetic/profiler-b", True, True),
    ({}, None, "synthetic/profiler-b", False, True),
    ({}, "synthetic/chat-a", "", True, False),
    ({"expectedModel": "synthetic/legacy"}, "synthetic/legacy", "synthetic/legacy", True, True),
    ({"expectedModel": "synthetic/legacy"}, "synthetic/legacy", "synthetic/other", True, False),
    ({"expectedChatModel": "synthetic/chat-a"}, "synthetic/chat-a", "synthetic/profiler-b", True, True),
    ({"expectedChatModel": "synthetic/chat-a", "expectedProfilerModel": "synthetic/profiler-b",
      "expectedModel": "synthetic/legacy"}, "synthetic/chat-a", "synthetic/profiler-b", True, True),
    ({"expectedChatModel": "synthetic/chat-a", "expectedProfilerModel": "synthetic/profiler-b"},
     "synthetic/wrong", "synthetic/profiler-b", False, True),
    ({"expectedChatModel": "synthetic/chat-a", "expectedProfilerModel": "synthetic/profiler-b"},
     "synthetic/chat-a", "synthetic/wrong", True, False),
])
def test_separate_model_expectations_apply_to_guards_and_snapshot(monkeypatch, settings, chat_model, profiler_model, chat_ok, profiler_ok):
    from fastapi import HTTPException

    class DB:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query):
            assert query in {"SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout='2000ms'"} or query.startswith("SELECT current_database(),database_name,version")
            return self
        def fetchone(self): return ("emmet4930_synthetic", "emmet4930_synthetic", server.DATABASE_VERSION)

    monkeypatch.setattr(server.psycopg, "connect", lambda *args, **kwargs: DB())

    async def case():
        backend = server.Backend({"liveModel": True, "budgetMode": "provider-key-limits",
            "database": "postgresql://synthetic@127.0.0.1/emmet4930_synthetic",
            "targets": {"chat": "http://127.0.0.1:1", "profiling": "http://127.0.0.1:2"}, **settings})

        async def request(service, method, path, body=None):
            assert method == "GET"
            if service == "chat":
                return 200, {"ready": True, "database": "postgresql", "mode": "live",
                             "model": chat_model, "budgetMode": "provider-key-limits"}
            if service == "profiling":
                if path == "/readyz":
                    return 200, {"ready": True, "checks": {key: True for key in
                        ("catalog", "db", "migration", "integration_required", "required_embedding")}}
                return 200, {"tagger": {"enabled": True, "required": True, "model": profiler_model},
                             "integration": {"budgetGuard": "provider-key-limits"},
                             "embedding": {"enabled": True}, "store": {"connected": True}, "catalog": {"active": True}}
            return 200, {"status": "ready", "model": "synthetic-encoder"}

        backend.request = request
        monkeypatch.setattr(backend, "database_snapshot", lambda: {})
        try:
            for check, allowed in [(backend.check_chat_ready, chat_ok),
                                   (backend.check_profile_ready, profiler_ok)]:
                if allowed:
                    await check()
                else:
                    with pytest.raises(HTTPException) as rejected:
                        await check()
                    assert rejected.value.status_code == 409
            snapshot = await backend.snapshot()
            assert (snapshot["services"]["chat"]["status"] == "ready") is chat_ok
            assert (snapshot["services"]["profiling"]["status"] == "ready") is profiler_ok
            assert snapshot["runtimeVerified"] is chat_ok
        finally:
            await backend.http.aclose()
    asyncio.run(case())




@pytest.mark.parametrize("fault", [None, "chat_down", "not_ready", "wrong_model", "wrong_database",

    "profile_down", "profile_store_down", "profile_catalog_missing", "profile_shape",
    "profile_metadata", "profile_model", "profile_enabled", "profile_not_ready", "profile_migration", "profile_ready_shape",
    "wrong_identity", "wrong_db_version", "missing_identity"])
def test_stale_pid_resume_and_faults_never_admit_unverified_sends(monkeypatch, fault):
    monkeypatch.setattr(server.os, "kill", lambda *args: pytest.fail("Live readiness must not depend on a launcher PID"))
    class DB:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query):
            assert query in {'SET TRANSACTION READ ONLY', "SET LOCAL statement_timeout='2000ms'"} or query.startswith('SELECT current_database(),database_name,version FROM ai_runtime.database_identity')
            if fault == 'missing_identity': raise server.psycopg.errors.UndefinedTable('synthetic missing identity')
            return self
        def fetchone(self):
            return ('emmet4930_synthetic', 'wrong' if fault == 'wrong_identity' else 'emmet4930_synthetic',
                    'single-server/obsolete' if fault == 'wrong_db_version' else server.DATABASE_VERSION)
    monkeypatch.setattr(server.psycopg, 'connect', lambda *args, **kwargs: DB())
    async def case():
        config = {'liveModel': True, 'budgetMode': 'guarded', 'launcherPid': 2**30, 'database': 'postgresql://synthetic@127.0.0.1/emmet4930_synthetic',
                  'expectedModel': 'openai/gpt-6-luna', 'budgetAccount': 'gift-chat-harness-2026-10-06', 'budgetExpectedLimitUsd': 5,
                  'targets': {'chat': 'http://127.0.0.1:1', 'profiling': 'http://127.0.0.1:2'}}
        app = configured_app(config)
        mutations, accepted_turns, accepted_profiles = [], set(), set()
        def upstream(request):
            path = request.url.path
            if request.method == 'GET' and path == '/readyz' and request.url.port == 2:
                if fault == 'profile_ready_shape': return httpx.Response(200, json=[])
                checks = {key: True for key in ('catalog', 'db', 'migration', 'integration_required', 'required_embedding')}
                checks['migration'] = fault != 'profile_migration'
                return httpx.Response(200, json={'ready': fault != 'profile_not_ready', 'checks': checks})
            if request.method == 'GET' and path == '/readyz':
                if fault == 'chat_down': raise httpx.ConnectError('synthetic down')
                data = {'ready': fault != 'not_ready', 'mode': 'live', 'database': 'postgresql',
                        'model': config['expectedModel'], 'budgetAccount': 'gift-chat-harness-2026-10-06',
                        'sharedBudget': {'limitUsd': 5, 'remainingUsd': 4, 'halted': None}}
                if fault == 'wrong_model': data['model'] = 'other-model'
                if fault == 'wrong_database': data['database'] = 'sqlite'
                return httpx.Response(200, content=json.dumps(data))
            if request.method == 'GET' and path == '/health':
                if fault == 'profile_down': return httpx.Response(503, json={})
                if fault == 'profile_shape': return httpx.Response(200, json=[])
                data = {'tagger': {'enabled': True, 'required': True, 'model': config['expectedModel']},
                    'integration': {'budgetGuard': 'central-account'},
                    'store': {'connected': fault != 'profile_store_down'}, 'catalog': {'active': fault != 'profile_catalog_missing'},
                    'embedding': {'enabled': True}}
                if fault == 'profile_metadata': data['store'] = None
                if fault == 'profile_model': data['tagger']['model'] = 'wrong-model'
                if fault == 'profile_enabled': data['embedding']['enabled'] = 'true'
                return httpx.Response(200, json=data)
            if request.method == 'GET' and '/turns/' in path:
                return httpx.Response(200, json={'data': {'status': 'PROCESSING'}})
            mutations.append((request.method, path))
            if '/turns/' in path:
                accepted_turns.add(path)
                return httpx.Response(202, json={'data': {'status': 'PROCESSING'}})
            if path.endswith('extract-and-pool'):
                body = json.loads(request.content)
                accepted_profiles.add((body['recipientUserId'], body['sourceVersion']))
                return httpx.Response(202, json={'data': {'profileStatus': 'PENDING'}})
            return httpx.Response(201, json={'data': {}})
        await app.state.backend.http.aclose()
        app.state.backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        profile_ok = fault is None or fault in {'chat_down','not_ready','wrong_model','wrong_database'}
        chat_ok = fault is None or fault.startswith('profile_') or fault in {'wrong_identity', 'wrong_db_version', 'missing_identity'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1:9000',
                                     headers={'Origin': 'http://127.0.0.1:9000'}) as client:
            await login(client)
            policy = (await client.get('/api/config')).json()
            assert policy['chatSendAllowed'] == chat_ok
            assert policy['runtimeVerified'] == chat_ok
            assert policy['profileSendAllowed'] == profile_ok
            body = {k: str(uuid.uuid4()) for k in ['request_id', 'conversation_id', 'turn_id']} | {'message': 'SYNTHETIC FREE CONTRACT'}
            for _ in range(2):
                chat = await client.post('/api/chat', json=body)
                profile = await client.post('/api/profile', json={'recipientUserId': 42, 'sourceVersion': 99, 'giftPreference': 'SYNTHETIC'})
                assert (chat.status_code == 200 and chat.json()['httpStatus'] == 202) == chat_ok
                assert (profile.status_code == 200 and profile.json()['httpStatus'] == 202) == profile_ok
            assert len(accepted_turns) == int(chat_ok)
            assert len(accepted_profiles) == int(profile_ok)
            if not chat_ok: assert not any('/turns/' in path for _,path in mutations)
            if not profile_ok: assert not any(method == 'POST' for method, _ in mutations)
        await app.state.backend.http.aclose()
    asyncio.run(case())


@pytest.mark.parametrize("mode", [None, "provider-key-limits", "guarded"])
def test_profiler_provider_mode_does_not_probe_an_unavailable_chat(monkeypatch, mode):
    from fastapi import HTTPException
    class DB:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query): return self
        def fetchone(self): return ("emmet4930_synthetic", "emmet4930_synthetic", server.DATABASE_VERSION)
    monkeypatch.setattr(server.psycopg, "connect", lambda *a, **kw: DB())
    async def case():
        probes = []
        config = {"liveModel": True, "database": "postgresql://synthetic@127.0.0.1/emmet4930_synthetic",
                  "targets": {"chat": "http://127.0.0.1:1", "profiling": "http://127.0.0.1:2"}}
        if mode is not None: config["budgetMode"] = mode
        backend = server.Backend(config)
        async def request(service, method, path, body=None):
            probes.append((service, path))
            if service == "chat": return 503, {"ready": False}
            if path == "/readyz":
                return 200, {"ready": True, "checks": {key: True for key in
                    ("catalog", "db", "migration", "integration_required", "required_embedding")}}
            return 200, {"integration": {"budgetGuard": "provider-key-limits"},
                "tagger": {"enabled": True, "required": True, "model": "synthetic/profiler"},
                "embedding": {"enabled": True}, "store": {"connected": True}, "catalog": {"active": True}}
        backend.request = request
        try:
            await backend.check_profile_ready()
            assert probes == [("profiling", "/readyz"), ("profiling", "/health")]
        finally: await backend.http.aclose()
    asyncio.run(case())
