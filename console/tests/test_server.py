import asyncio
import json
import uuid

import httpx
import pytest

from console.server import create_app, http_masked, masked, target_url


def run(function):
    return asyncio.run(function())


def test_static_build_is_an_immutable_startup_snapshot(tmp_path, monkeypatch):
    from console import server

    static = tmp_path / "static"
    static.mkdir()
    for name in ("index.html", "app.js", "core.js", "ops.js", "style.css"):
        (static / name).write_text("SYNTHETIC original " + name)
    monkeypatch.setattr(server, "ROOT", tmp_path)

    async def case():
        app = create_app()
        (static / "app.js").write_text("SYNTHETIC changed")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000"
        ) as http:
            config = (await http.get("/api/config")).json()
            response = await http.get("/static/app.js")
            assert response.text == "SYNTHETIC original app.js"
            assert response.headers["x-console-build"] == config["build"]["id"]
            assert (await http.get("/static/missing.js")).status_code == 404
        await app.state.backend.http.aclose()

    run(case)


def test_target_restriction_and_credentials_never_exposed():
    for value in [
        "https://example.com",
        "http://169.254.169.254",
        "http://user:secret@127.0.0.1",
        "http://127.0.0.1/private",
    ]:
        with pytest.raises(ValueError):
            target_url(value)
    assert masked(
        {"authorization": "Bearer secret", "text": "Bearer not-a-real-token"}
    ) == {"authorization": "[redacted]", "text": "[redacted]"}


def test_chat_idempotency_cancel_events_and_origin_boundary():
    async def case():
        app = create_app()
        backend = app.state.backend
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000"
        ) as http:
            body = {
                k: str(uuid.uuid4())
                for k in ["request_id", "conversation_id", "turn_id"]
            } | {"message": "/mock slow"}
            assert (await http.post("/api/chat", json=body)).status_code == 403
            headers = {"Origin": "http://127.0.0.1:9000"}
            first = await http.post("/api/chat", json=body, headers=headers)
            second = await http.post("/api/chat", json=body, headers=headers)
            assert (
                first.json()["turnId"] == second.json()["turnId"]
                and len(backend.turns) == 1
            )
            assert (
                await http.post(
                    "/api/chat", json=body | {"message": "changed"}, headers=headers
                )
            ).status_code == 409
            tid = body["turn_id"]
            await http.put(
                "/api/chat/" + tid + "/cancellation", json={}, headers=headers
            )
            assert (await http.get("/api/chat/" + tid)).json()["data"][
                "status"
            ] == "CANCELLED"
            events = await http.get("/api/chat/" + tid + "/events?afterEventId=0")
            assert "turn.finished" in events.text
            assert (await http.get("/api/chat/" + str(uuid.uuid4()))).status_code == 404
            assert (
                await http.get("/api/config", headers={"Host": "evil.example"})
            ).status_code == 403
        for task in backend.jobs:
            task.cancel()
        await asyncio.gather(*backend.jobs, return_exceptions=True)
        await backend.http.aclose()

    run(case)


def test_profile_failure_and_snapshot_is_not_console_timeline():
    async def case():
        app = create_app()
        backend = app.state.backend
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000"
        ) as http:
            headers = {"Origin": "http://127.0.0.1:9000"}
            body = {
                "recipientUserId": 42,
                "sourceVersion": 1,
                "giftPreference": "/fail SYNTHETIC",
            }
            response = await http.post("/api/profile", json=body, headers=headers)
            assert response.json()["httpStatus"] == 202
            assert (
                await http.post(
                    "/api/profile",
                    json=body | {"giftPreference": "changed"},
                    headers=headers,
                )
            ).status_code == 409
            await asyncio.sleep(0.8)
            state = (await http.get("/api/profile/42/1")).json()
            assert state["data"]["status"] == "FAILED"
            snapshot = (await http.get("/api/snapshot")).json()
            assert snapshot["profileCounts"] == {"FAILED": 1}
            assert (
                snapshot["budget"] is None and "Server TTFT" in snapshot["unmeasured"]
            )
            assert "SYNTHETIC" not in json.dumps(snapshot["jobs"])
            assert (await http.get("/api/profile/43/1")).status_code == 404
        await backend.http.aclose()

    run(case)


def test_live_unknown_cost_policy_is_blocked_and_config_has_no_tokens():
    async def case():
        app = create_app(
            {
                "targets": {"profiling": "http://127.0.0.1:12345"},
                "tokens": {"profiling": "PRIVATE_TOKEN_CANARY"},
            }
        )
        await app.state.backend.http.aclose()
        async def unavailable(request): raise httpx.ConnectError("synthetic unavailable")
        app.state.backend.http=httpx.AsyncClient(transport=httpx.MockTransport(unavailable))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000"
        ) as http:
            response = await http.post(
                "/api/profile",
                json={"recipientUserId": 1, "sourceVersion": 1},
                headers={"Origin": "http://127.0.0.1:9000"},
            )
            assert response.status_code == 409
            assert "PRIVATE_TOKEN_CANARY" not in (await http.get("/api/config")).text
            snapshot = (await http.get("/api/snapshot")).json()
            assert snapshot["budget"] is None and snapshot["jobs"] is None
            assert snapshot["services"]["profiling"]["status"] == "unknown"
        await app.state.backend.http.aclose()

    run(case)


def test_retry_reaches_upstream_with_same_uuid_and_body_without_new_turn():
    async def case():
        app = create_app({"targets": {"chat": "http://127.0.0.1:12345"}})
        backend = app.state.backend
        calls = []

        def upstream(request):
            calls.append((request.method, request.url.path, request.content))
            if request.url.path == "/readyz":
                return httpx.Response(200, json={"mode": "mock"})
            if "/turns/" not in request.url.path:
                return httpx.Response(200, json={"data": {"turns": []}})
            turn_calls = [c for c in calls if "/turns/" in c[1]]
            return httpx.Response(
                202 if len(turn_calls) == 1 else 200,
                json={
                    "data": {
                        "status": "PROCESSING" if len(turn_calls) == 1 else "COMPLETED"
                    }
                },
            )

        await backend.http.aclose()
        backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000"
        ) as http:
            body = {
                k: str(uuid.uuid4())
                for k in ["request_id", "conversation_id", "turn_id"]
            } | {"message": "/mock cards"}
            headers = {"Origin": "http://127.0.0.1:9000"}
            first = (await http.post("/api/chat", json=body, headers=headers)).json()
            second = (await http.post("/api/chat", json=body, headers=headers)).json()
            turns = [c for c in calls if "/turns/" in c[1]]
            assert len(turns) == 2 and turns[0] == turns[1]
            assert first["httpStatus"] == 202 and second["httpStatus"] == 200
            assert len(backend.admissions) == 1
        await backend.http.aclose()

    run(case)


@pytest.mark.parametrize("upstream_status", [202, 429, 503])
def test_http_observation_distinguishes_relay_and_upstream_and_redacts(upstream_status):
    async def case():
        app = create_app(
            {
                "targets": {"chat": "http://127.0.0.1:12345"},
                "tokens": {"chat": "PRIVATE_AUTH_TOKEN"},
            }
        )
        backend = app.state.backend

        def upstream(request):
            if request.url.path == "/readyz":
                return httpx.Response(200, json={"mode": "mock"})
            if "/turns/" not in request.url.path:
                return httpx.Response(201, json={"data": {"turns": []}})
            return httpx.Response(
                upstream_status,
                json={"error": {"code": "SYNTHETIC_STATUS", "message": "PRIVATE_BODY"}},
                headers={
                    "Retry-After": "2",
                    "Set-Cookie": "PRIVATE_COOKIE",
                    "Authorization": "Bearer PRIVATE_AUTH",
                },
            )

        await backend.http.aclose()
        backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000"
        ) as http:
            body = {
                k: str(uuid.uuid4())
                for k in ["request_id", "conversation_id", "turn_id"]
            } | {"message": "PRIVATE_REQUEST"}
            response = await http.post(
                "/api/chat", json=body, headers={"Origin": "http://127.0.0.1:9000"}
            )
            assert (
                response.status_code == 200
                and response.json()["httpStatus"] == upstream_status
            )
            trace = response.json()["transport"]["exchanges"]
            assert [e["status"] for e in trace] == [200, 201, upstream_status]
            assert trace[-1]["headers"]["retry-after"] == "2"
            assert "PRIVATE_" not in json.dumps(trace)
            assert (
                "authorization" not in json.dumps(trace).lower()
                and "set-cookie" not in json.dumps(trace).lower()
            )
            assert all(e["durationMs"] >= 0 for e in trace)
        await backend.http.aclose()

    run(case)


def test_upstream_connection_failure_has_no_invented_status():
    async def case():
        app = create_app({"targets": {"chat": "http://127.0.0.1:12345"}})
        backend = app.state.backend

        def unavailable(request):
            raise httpx.ConnectError("PRIVATE_CONNECTION_DETAIL", request=request)

        await backend.http.aclose()
        backend.http = httpx.AsyncClient(transport=httpx.MockTransport(unavailable))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000"
        ) as http:
            body = {
                k: str(uuid.uuid4())
                for k in ["request_id", "conversation_id", "turn_id"]
            } | {"message": "/mock cards"}
            response = await http.post(
                "/api/chat", json=body, headers={"Origin": "http://127.0.0.1:9000"}
            )
            assert response.status_code == 502
            exchange = response.json()["transport"]["exchanges"][0]
            assert exchange["status"] is None and exchange["error"] == "ConnectError"
            assert "PRIVATE_CONNECTION_DETAIL" not in response.text
        await backend.http.aclose()

    run(case)


@pytest.mark.parametrize("stream_status", [200, 429, 503, None])
def test_sse_handshake_observes_actual_upstream_or_missing_response(stream_status):
    async def case():
        app = create_app({"targets": {"chat": "http://127.0.0.1:12345"}})
        backend = app.state.backend

        def upstream(request):
            if request.url.path == "/readyz":
                return httpx.Response(200, json={"mode": "mock"})
            if request.url.path.endswith("/events"):
                if stream_status is None:
                    raise httpx.ConnectError("PRIVATE_CONNECTION", request=request)
                if stream_status == 200:
                    return httpx.Response(
                        200,
                        text='id: 1\nevent: turn.finished\ndata: {"status":"COMPLETED"}\n\n',
                        headers={"Content-Type": "text/event-stream"},
                    )
                return httpx.Response(
                    stream_status,
                    json={
                        "error": {
                            "code": "SYNTHETIC_OVERLOAD",
                            "message": "PRIVATE_ERROR",
                        }
                    },
                    headers={"Retry-After": "2", "Set-Cookie": "PRIVATE_COOKIE"},
                )
            return httpx.Response(
                202 if "/turns/" in request.url.path else 201,
                json={"data": {"status": "PROCESSING"}},
            )

        await backend.http.aclose()
        backend.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000"
        ) as http:
            body = {
                k: str(uuid.uuid4())
                for k in ["request_id", "conversation_id", "turn_id"]
            } | {"message": "/mock cards"}
            await http.post(
                "/api/chat", json=body, headers={"Origin": "http://127.0.0.1:9000"}
            )
            response = await http.get(
                "/api/chat/" + body["turn_id"] + "/events?afterEventId=0"
            )
            assert response.status_code == 200
            data = json.loads(
                next(
                    line[6:]
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                )
            )
            exchange = data["exchanges"][0]
            assert exchange["status"] == stream_status
            assert exchange["requestBody"] is None and exchange["query"] == {
                "afterEventId": 0
            }
            assert "PRIVATE_" not in json.dumps(data)
            if stream_status in (429, 503):
                assert exchange["headers"]["retry-after"] == "2"
            if stream_status is None:
                assert exchange["error"] == "ConnectError"
        await backend.http.aclose()

    run(case)


def test_validation_context_is_permanently_masked_in_upstream_http_trace():
    value = {
        "detail": [
            {
                "type": "string_type",
                "loc": ["body", "giftPreference"],
                "input": "SYNTHETIC_PRIVATE_CANARY",
                "ctx": {"error": "SYNTHETIC_PRIVATE_CANARY"},
            }
        ]
    }
    safe = http_masked(value)
    assert "SYNTHETIC_PRIVATE_CANARY" not in json.dumps(safe)
    assert safe["detail"][0]["input"] == "[원문 숨김]"
    assert safe["detail"][0]["ctx"] == "[원문 숨김]"
    assert safe["detail"][0]["type"] == "string_type"


def test_real_console_422_echo_is_masked_for_http_history():
    async def case():
        app = create_app()
        backend = app.state.backend
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000"
        ) as http:
            response = await http.post(
                "/api/profile",
                json={
                    "recipientUserId": 42,
                    "sourceVersion": 1,
                    "giftPreference": {"raw": "SYNTHETIC_PRIVATE_CANARY"},
                },
                headers={"Origin": "http://127.0.0.1:9000"},
            )
            assert response.status_code == 422
            # Validate the actual FastAPI error shape rather than an assumed fixture.
            assert "SYNTHETIC_PRIVATE_CANARY" in json.dumps(response.json())
            sanitized = http_masked(response.json())
            assert "SYNTHETIC_PRIVATE_CANARY" not in json.dumps(sanitized)
            assert sanitized["detail"][0]["type"] == "string_type"
            assert sanitized["detail"][0]["loc"] == ["body", "giftPreference"]
            assert sanitized["detail"][0]["input"] == "[원문 숨김]"
        await backend.http.aclose()

    run(case)


@pytest.mark.parametrize("lost_ack", [False, True])
def test_live_retry_freezes_enriched_body_before_first_put(lost_ack):
    from console.server import Backend, ChatInput

    async def case():
        backend = Backend(
            {
                "liveModel": True,
                "targets": {
                    "chat": "http://127.0.0.1:1",
                    "backend": "http://127.0.0.1:2",
                },
            }
        )
        await backend.http.aclose()
        contexts, submitted, paths = [], [], []

        async def budget():
            return {"mode": "live"}

        backend.check_live_budget = budget

        def remote(request):
            path = request.url.path
            paths.append(path)
            if path == "/readyz":
                return httpx.Response(200, json={"mode": "live"})
            if path.startswith("/fixture/"):
                version = len(contexts) + 1
                contexts.append(version)
                return httpx.Response(
                    200,
                    json={
                        "synthetic": True,
                        "recipientContext": {
                            "recipientUserId": 42,
                            "sourceVersion": version,
                        },
                        "preparedContext": {
                            "productIds": [version],
                            "snapshotId": "synthetic",
                        },
                    },
                )
            if "/turns/" not in path:
                return httpx.Response(201, json={"data": {}})
            body = json.loads(request.content)
            submitted.append(body)
            if lost_ack and len(submitted) == 1:
                raise httpx.ReadError("synthetic acknowledgement lost", request=request)
            if body != submitted[0]:
                return httpx.Response(409, json={"message": "changed body"})
            return httpx.Response(
                202 if len(submitted) == 1 else 200, json={"data": {}}
            )

        backend.http = httpx.AsyncClient(transport=httpx.MockTransport(remote))
        data = ChatInput(
            request_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            turn_id=uuid.uuid4(),
            message="synthetic",
            recipientUserId=42,
        )
        try:
            if lost_ack:
                with pytest.raises(httpx.ReadError):
                    await backend.chat(data)
            else:
                assert (await backend.chat(data))["httpStatus"] == 202
            assert (await backend.chat(data))["httpStatus"] == 200
            assert contexts == [1]
            assert submitted[0] == submitted[1]
            assert (
                backend.admissions[str(data.request_id)]["body"]["recipientContext"][
                    "sourceVersion"
                ]
                == 1
            )
            assert all("/test/" not in path for path in paths)
            with pytest.raises(Exception) as error:
                await backend.chat(data.model_copy(update={"message": "changed"}))
            assert error.value.status_code == 409
            assert len(submitted) == 2
        finally:
            await backend.http.aclose()

    run(case)


@pytest.mark.parametrize("failure", ["read_error", "incomplete_eof"])
def test_midframe_disconnect_keeps_only_complete_sse_cursor_for_replay(failure):
    from console.server import Backend

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'id: 1\nevent: answer.delta\ndata: {"delta":"first"}\n\n'
            yield b'id: 2\nevent: answer.delta\ndata: {"delta":"partial'
            if failure == "read_error":
                raise httpx.ReadError("synthetic midframe connection loss")

    async def case():
        backend = Backend(
            {"liveModel": True, "targets": {"chat": "http://127.0.0.1:1"}}
        )
        tid, cid = str(uuid.uuid4()), str(uuid.uuid4())
        backend.admissions["synthetic"] = {
            "body": {"turn_id": tid, "conversation_id": cid}
        }
        seen = []

        def remote(request):
            seen.append(
                (
                    request.method,
                    request.url.path,
                    request.url.params.get("afterEventId"),
                )
            )
            if len(seen) == 1:
                return httpx.Response(200, stream=BrokenStream())
            return httpx.Response(
                200,
                text='id: 2\nevent: turn.finished\ndata: {"status":"COMPLETED"}\n\n',
            )

        await backend.http.aclose()
        backend.http = httpx.AsyncClient(transport=httpx.MockTransport(remote))
        try:
            first = "".join([chunk async for chunk in backend.events(tid, 0)])
            assert "id: 1\n" in first
            assert (
                "id: 2\n" not in first
            )  # incomplete frames cannot move the browser cursor
            assert "event: console.error\n" in first
            for frame in first.split("\n\n"):
                for line in frame.splitlines():
                    if line.startswith("data: "):
                        json.loads(
                            line[6:]
                        )  # relay diagnostics cannot merge into partial JSON
            replay = "".join([chunk async for chunk in backend.events(tid, 1)])
            assert "id: 2\nevent: turn.finished" in replay
            assert [s[2] for s in seen] == ["0", "1"]
            assert all(
                method == "GET" and "/test/" not in path for method, path, _ in seen
            )
        finally:
            await backend.http.aclose()

    run(case)


@pytest.mark.parametrize("oversized", [False, True])
def test_sse_frame_limit_and_split_crlf_do_not_create_false_event(oversized):
    from console.server import Backend

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            if oversized:
                yield b"id: 99\ndata: " + b"x" * 1048577
            else:
                yield b"id: 1\r"
                yield b'\nevent: turn.finished\r\ndata: {"status":"COMPLETED"}\r'
                yield b"\n\r\n"

    async def case():
        backend = Backend(
            {"liveModel": True, "targets": {"chat": "http://127.0.0.1:1"}}
        )
        tid = str(uuid.uuid4())
        backend.admissions["synthetic"] = {
            "body": {"turn_id": tid, "conversation_id": str(uuid.uuid4())}
        }
        await backend.http.aclose()
        backend.http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=Chunks())
            )
        )
        try:
            wire = "".join([chunk async for chunk in backend.events(tid, 0)])
            if oversized:
                assert "id: 99\n" not in wire and "event: console.error\n" in wire
                assert len(wire) < 4000
            else:
                assert (
                    'id: 1\nevent: turn.finished\ndata: {"status":"COMPLETED"}\n\n'
                    in wire
                )
                assert "event: console.error\n" not in wire
        finally:
            await backend.http.aclose()

    run(case)


@pytest.mark.parametrize('binding_status', [200, 409, 503])
def test_live_profile_input_is_bound_before_provider_admission(monkeypatch, binding_status):
    from console import server
    class DB:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args): return self
        def fetchone(self): return (1,)
    monkeypatch.setattr(server.psycopg, 'connect', lambda *a, **k: DB())
    async def case():
        backend = server.Backend({'liveModel': True, 'database': 'synthetic', 'launcherPid': __import__('os').getpid(), 'targets': {'chat': 'http://127.0.0.1:1', 'backend': 'http://127.0.0.1:2', 'profiling': 'http://127.0.0.1:3'}})
        calls = []
        async def budget(): return {}
        async def request(service, method, path, body=None):
            calls.append((service, method, path, body))
            if path == '/health': return 200, {'tagger': {'enabled': True, 'required': True}, 'embedding': {'enabled': True}}
            if service == 'backend': return binding_status, {'synthetic': True, 'inputBound': binding_status == 200}
            return 202, {'data': {'profileStatus': 'PENDING'}}
        backend.check_live_budget = budget
        backend.request = request
        body = server.ProfileInput(recipientUserId=42, sourceVersion=2, giftPreference='SYNTHETIC')
        try:
            result = await backend.profile(body)
            assert result['httpStatus'] == (202 if binding_status == 200 else binding_status)
            assert calls[1][:3] == ('backend', 'PUT', '/fixture/recipients/42/profile-input')
            assert calls[1][3] == body.model_dump()
            assert any(c[0] == 'profiling' and c[1] == 'POST' for c in calls) == (binding_status == 200)
            assert ('42:2' in backend.profile_inputs) == (binding_status == 200)
        finally: await backend.http.aclose()
    run(case)


def test_profile_observation_survives_console_restart_for_owned_synthetic_recipient_only():
    from console import server
    async def case():
        backend = server.Backend({'liveModel': True, 'defaultRecipientUserId': 42, 'database': 'synthetic', 'targets': {}})
        calls=[]
        def read(key):
            calls.append(key)
            return {'status': 'DELIVERED', 'callbackAttempts': 1} if key == '42:1' else None
        backend.read_profile = read
        try:
            result = await backend.profile_state('42:1')
            assert result['data']['status'] == 'DELIVERED'
            assert result['recoveredObservation'] is True
            assert not backend.profile_inputs  # observation does not authorize new provider work
            for key in ['43:1', '42:99']:
                with pytest.raises(server.HTTPException) as error: await backend.profile_state(key)
                assert error.value.status_code == 404
            assert calls == ['42:1','42:99']
        finally: await backend.http.aclose()
    run(case)


def test_invalid_profile_is_rejected_before_any_admission_or_binding():
    async def case():
        app = create_app()
        backend = app.state.backend
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1:9000') as http:
            headers = {'Origin': 'http://127.0.0.1:9000'}
            valid = {'recipientUserId': 42, 'sourceVersion': 2, 'reviews': [], 'dislikedCategories': []}
            for invalid in [{'reviews':[{}]}, {'dislikedCategories':[{'categoryId':i,'categoryName':'SYNTHETIC'} for i in range(1,7)]}]:
                assert (await http.post('/api/profile',json=valid|invalid,headers=headers)).status_code == 422
                assert not backend.profile_inputs and not backend.profiles
            assert (await http.post('/api/profile',json=valid,headers=headers)).json()['httpStatus'] == 202
        for task in backend.jobs: task.cancel()
        await asyncio.gather(*backend.jobs,return_exceptions=True)
        await backend.http.aclose()
    run(case)


def test_upstream_chat_state_failure_keeps_failure_http_status():
    async def case():
        app = create_app({'targets': {'chat': 'http://127.0.0.1:1'}})
        backend = app.state.backend
        tid,cid = str(uuid.uuid4()),str(uuid.uuid4())
        backend.admissions['synthetic']={'body':{'turn_id':tid,'conversation_id':cid}}
        async def request(*args,**kwargs): return 404,{'error':{'code':'NOT_FOUND'}}
        backend.request=request
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1:9000') as http:
            response=await http.get('/api/chat/'+tid)
            assert response.status_code == 404
            assert 'data' not in response.json()
        await backend.http.aclose()
    run(case)
