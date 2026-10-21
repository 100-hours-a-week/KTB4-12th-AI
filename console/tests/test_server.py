import asyncio
import hashlib
import json
import uuid

import httpx
import pytest

from console.server import http_masked, masked, target_url
from console.tests.auth_support import configured_app as create_app, login


def run(function):
    return asyncio.run(function())


@pytest.mark.parametrize("fault", [None, "status", "mode", "account", "limit", "halted", "missing"])
def test_managed_console_keeps_actual_cumulative_budget_gate_without_launcher(fault, monkeypatch):
    from fastapi import HTTPException
    from console.server import Backend
    monkeypatch.setattr("console.server.os.kill", lambda *args:
                        pytest.fail("Managed containers do not share launcher process IDs"))
    async def case():
        backend = Backend({"managedDeployment": True, "liveModel": True,
                           "budgetMode": "guarded",
                           "budgetAccount": "gift-chat-harness-2026-10-06", "budgetExpectedLimitUsd": 5})
        ready = {"ready": True, "database": "postgresql", "model": "synthetic-model", "mode": "live", "budgetAccount": "gift-chat-harness-2026-10-06",
                 "sharedBudget": {"limitUsd": 5, "halted": None, "remainingUsd": 4}}
        if fault == "mode": ready["mode"] = "mock"
        if fault == "account": ready["budgetAccount"] = "different-budget"
        if fault == "limit": ready["sharedBudget"]["limitUsd"] = 10
        if fault == "halted": ready["sharedBudget"]["halted"] = "exhausted"
        if fault == "missing": ready.pop("sharedBudget")
        calls = []
        async def request(*args):
            calls.append(args)
            return (503 if fault == "status" else 200), ready
        backend.request = request
        try:
            if fault:
                with pytest.raises(HTTPException) as error:
                    await backend.check_live_budget()
                assert error.value.status_code == 409
            else:
                assert await backend.check_live_budget() == ready
            assert calls == [("chat", "GET", "/readyz")]
        finally:
            await backend.http.aclose()
    run(case)


def test_static_build_is_an_immutable_startup_snapshot(tmp_path, monkeypatch):
    from console import server

    static = tmp_path / "static"
    static.mkdir()
    for name in ("index.html", "app.js", "core.js", "ops.js", "products.js", "style.css", "login.html", "login.js", "admin.js", "usage.js"):
        (static / name).write_text("SYNTHETIC original " + name)
    monkeypatch.setattr(server, "ROOT", tmp_path)

    async def case():
        app = create_app()
        (static / "app.js").write_text("SYNTHETIC changed")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000"
        ) as http:
            await login(http)
            config = (await http.get("/api/config")).json()
            response = await http.get("/static/app.js")
            assert response.text == "SYNTHETIC original app.js"
            assert response.headers["x-console-build"] == config["build"]["id"]
            assert (await http.get("/static/missing.js")).status_code == 404
        await app.state.backend.http.aclose()

    run(case)


@pytest.mark.parametrize("mode", [None, "guarded", "provider-key-limits", "unknown", ""])
@pytest.mark.parametrize("ready_ok", [True, False])
@pytest.mark.parametrize("old_ledger", [None, {"limitUsd": 5, "remainingUsd": 0, "halted": "exhausted"}])
def test_default_provider_limits_requires_healthy_matching_live_runtime(mode, ready_ok, old_ledger):
    from fastapi import HTTPException
    from console.server import Backend

    async def case():
        config = {"managedDeployment": True, "liveModel": True}
        if mode is not None:
            config["budgetMode"] = mode
        backend = Backend(config)
        ready = {"ready": ready_ok, "database": "postgresql", "mode": "live", "model": "synthetic-model", "budgetMode": "provider-key-limits"}
        if old_ledger is not None:
            ready["sharedBudget"] = old_ledger

        async def request(*args):
            assert args == ("chat", "GET", "/readyz")
            return 200, ready

        backend.request = request
        try:
            if mode in (None, "provider-key-limits") and ready_ok:
                assert await backend.check_live_budget() == ready
            else:
                with pytest.raises(HTTPException) as error:
                    await backend.check_live_budget()
                assert error.value.status_code == 409
        finally:
            await backend.http.aclose()
    run(case)


def test_catalog_import_is_excluded_and_operations_snapshot_remains():
    async def case():
        app = create_app()
        paths = {route.path for route in app.routes}
        assert "/api/snapshot" in paths
        assert not any(path.startswith("/api/catalog-import") for path in paths)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as http:
            await login(http)
            assert (await http.get("/api/snapshot")).status_code == 200
            assert (await http.get("/static/snapshot.js")).status_code == 404
            for path in ("/", "/static/app.js", "/static/style.css"):
                body = (await http.get(path)).text
                assert "snapshot-import" not in body and "mountSnapshotImport" not in body
        await app.state.backend.http.aclose()
    run(case)


@pytest.mark.parametrize("mode", [None, "guarded", "provider-key-limits"])
@pytest.mark.parametrize("reported", ["absent", "guarded", "provider-key-limits", None, "unknown", []])
def test_runtime_budget_policy_must_match_before_send_or_verified_display(mode, reported, monkeypatch):
    monkeypatch.delenv("AI_BUDGET_MODE", raising=False)
    async def case():
        config = {"liveModel": True, "targets": {"chat": "http://127.0.0.1:1"}}
        if mode is not None:
            config["budgetMode"] = mode
        app = create_app(config)
        backend = app.state.backend
        ready = {"ready": True, "database": "postgresql", "mode": "live", "model": "synthetic-chat",
                 "budgetAccount": "gift-chat-harness-2026-10-06",
                 "sharedBudget": {"limitUsd": 5, "remainingUsd": 4, "halted": None}}
        if reported != "absent":
            ready["budgetMode"] = reported

        async def request(service, method, path, body=None):
            assert method == "GET"
            return (200, ready) if service == "chat" else (503, {})

        backend.request = request
        expected = reported == (mode or "provider-key-limits") or mode == "guarded" and reported == "absent"
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as http:
                await login(http)
                policy = (await http.get("/api/config")).json()
                assert policy["chatSendAllowed"] is expected
                assert policy["budgetPolicyVerified"] is expected
                snapshot = (await http.get("/api/snapshot")).json()
                assert snapshot["budgetPolicyVerified"] is expected
                assert (snapshot["services"]["chat"]["status"] == "ready") is expected
        finally:
            await backend.http.aclose()
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


def test_private_mesh_targets_require_explicit_opt_in_and_keep_origin_boundary(monkeypatch):
    monkeypatch.delenv("CONSOLE_SERVICE_HOST", raising=False)
    with pytest.raises(ValueError):
        target_url("http://mesh:7860")
    monkeypatch.setenv("CONSOLE_SERVICE_HOST", "mesh")
    for port in (7860, 8000, 8001, 8002):
        assert target_url(f"http://mesh:{port}/") == f"http://mesh:{port}"
    for value in ("http://mesh:5432", "http://mesh", "https://mesh:7860", "http://mesh.example:7860",
                  "http://169.254.169.254:7860", "http://user:secret@mesh:7860", "http://@mesh:7860",
                  "http://mesh:7860/private", "http://mesh:7860?next=external", "http://mesh:7860#external"):
        with pytest.raises(ValueError):
            target_url(value)
    assert target_url("http://127.0.0.1:7860/") == "http://127.0.0.1:7860"


def test_chat_idempotency_cancel_events_and_origin_boundary():
    async def case():
        app = create_app()
        backend = app.state.backend
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000"
        ) as http:
            await login(http)
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
            await login(http)
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
            await login(http)
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


def test_retry_reads_same_upstream_turn_without_new_admission():
    async def case():
        app = create_app({"targets": {"chat": "http://127.0.0.1:12345"}})
        backend = app.state.backend
        calls = []

        def upstream(request):
            calls.append((request.method, request.url.path, request.content))
            if request.url.path == "/readyz":
                return httpx.Response(200, json={"mode": "mock"})
            if "/turns/" not in request.url.path:
                return httpx.Response(201, json={"data": {"turns": []}})
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
            await login(http)
            body = {
                k: str(uuid.uuid4())
                for k in ["request_id", "conversation_id", "turn_id"]
            } | {"message": "/mock cards"}
            headers = {"Origin": "http://127.0.0.1:9000"}
            first = (await http.post("/api/chat", json=body, headers=headers)).json()
            second = (await http.post("/api/chat", json=body, headers=headers)).json()
            turns = [c for c in calls if "/turns/" in c[1]]
            assert len(turns) == 2 and turns[0][0] == "PUT" and turns[1][0] == "GET"
            assert turns[0][1] == turns[1][1]
            assert first["httpStatus"] == 202 and second["httpStatus"] == 200
            with app.state.admin.session_store() as db:
                assert db.execute('SELECT count(*) FROM chat_admissions').fetchone()[0] == 1
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
            await login(http)
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
            await login(http)
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
            await login(http)
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
            await login(http)
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
            if request.method == "GET" and "/turns/" in path:
                return httpx.Response(404, json={"message": "synthetic absent turn"})
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


@pytest.mark.parametrize("stage", ["conversation", "turn"])
@pytest.mark.parametrize("outcome", [409, 429, 408, 503, "lost_ack", "write_error",
                                    "connect_error", "connect_timeout", "pool_timeout"])
def test_read_scope_requires_accepted_or_uncertain_turn_submission(stage, outcome):
    from console.server import Backend, ChatInput, HTTPException

    async def case():
        backend = Backend({"targets": {"chat": "http://127.0.0.1:1"}})
        data = ChatInput(request_id=uuid.uuid4(), conversation_id=uuid.uuid4(),
                         turn_id=uuid.uuid4(), message="synthetic")
        arrived, release = asyncio.Event(), asyncio.Event()
        fail = True
        turn_bodies = []

        async def request(service, method, path, body=None):
            if path == "/readyz":
                return 200, {"mode": "mock"}
            if method == "GET" and not fail and "/turns/" in path:
                return 404, {"message": "synthetic absent turn"}
            assert method == "PUT", "An unreadable turn must never reach upstream"
            current = "turn" if "/turns/" in path else "conversation"
            if current == "turn":
                turn_bodies.append(body)
            if fail and current == stage:
                arrived.set()
                await release.wait()
                if isinstance(outcome, str):
                    failure = {"lost_ack": httpx.ReadError, "write_error": httpx.WriteError,
                               "connect_error": httpx.ConnectError,
                               "connect_timeout": httpx.ConnectTimeout,
                               "pool_timeout": httpx.PoolTimeout}[outcome]
                    raise failure("synthetic transport failure")
                return outcome, {"error": {"code": "SYNTHETIC_REJECTION"}}
            return (202 if current == "turn" else 201), {"data": {}}

        async def assert_unreadable():
            for action in (backend.state(str(data.turn_id)),
                           backend.cancel(str(data.turn_id)),
                           backend.events(str(data.turn_id), 0).__anext__()):
                with pytest.raises(HTTPException) as error:
                    await action
                assert error.value.status_code == 404

        backend.request = request
        task = asyncio.create_task(backend.chat(data))
        try:
            await asyncio.wait_for(arrived.wait(), 2)
            await assert_unreadable()  # A future 409 cannot grant temporary access.
            release.set()
            if isinstance(outcome, str):
                with pytest.raises(httpx.HTTPError):
                    await task
            else:
                assert (await task)["httpStatus"] == outcome
            uncertain_turn = stage == "turn" and outcome in {408, 503, "lost_ack", "write_error"}
            if uncertain_turn:
                assert backend.admission(str(data.turn_id))["body"] == data.model_dump(mode="json")
            else:
                await assert_unreadable()
            if not isinstance(outcome, str) and not uncertain_turn:
                assert not backend.admissions
            fail = False
            assert (await backend.chat(data))["httpStatus"] == 202
            assert backend.admission(str(data.turn_id))["readable"] is True
            assert all(body == {"message": "synthetic"} for body in turn_bodies)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await backend.http.aclose()

    run(case)


@pytest.mark.parametrize("stage", ["conversation", "turn"])
def test_retry_rejection_preserves_previously_accepted_read_scope(stage):
    from console.server import Backend, ChatInput

    async def case():
        backend = Backend({"targets": {"chat": "http://127.0.0.1:1"}})
        data = ChatInput(request_id=uuid.uuid4(), conversation_id=uuid.uuid4(),
                         turn_id=uuid.uuid4(), message="synthetic")
        fail = False

        async def request(service, method, path, body=None):
            if path == "/readyz":
                return 200, {"mode": "mock"}
            if method == "GET" and "/turns/" in path:
                return 404, {"message": "synthetic absent turn"}
            current = "turn" if "/turns/" in path else "conversation"
            return (409 if fail and current == stage else
                    202 if current == "turn" else 201), {"data": {}}

        backend.request = request
        try:
            assert (await backend.chat(data))["httpStatus"] == 202
            accepted = backend.admission(str(data.turn_id))
            fail = True
            assert (await backend.chat(data))["httpStatus"] == 409
            assert backend.admission(str(data.turn_id)) is accepted
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
            "body": {"turn_id": tid, "conversation_id": cid}, "readable": True
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
            "body": {"turn_id": tid, "conversation_id": str(uuid.uuid4())}, "readable": True
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
        def fetchone(self): return ('synthetic', 'synthetic', server.DATABASE_VERSION)
    monkeypatch.setattr(server.psycopg, 'connect', lambda *a, **k: DB())
    async def case():
        backend = server.Backend({'liveModel': True, 'database': 'postgresql://synthetic@127.0.0.1/synthetic', 'launcherPid': __import__('os').getpid(), 'targets': {'chat': 'http://127.0.0.1:1', 'backend': 'http://127.0.0.1:2', 'profiling': 'http://127.0.0.1:3'}})
        calls = []
        async def budget(): return {}
        async def request(service, method, path, body=None):
            calls.append((service, method, path, body))
            if path == '/readyz': return 200, {'ready': True, 'checks': {key: True for key in ('catalog', 'db', 'migration', 'integration_required', 'required_embedding')}}
            if path == '/health': return 200, {'tagger': {'enabled': True, 'required': True, 'model': 'synthetic-profiler'}, 'integration': {'budgetGuard': 'provider-key-limits'}, 'embedding': {'enabled': True}, 'store': {'connected': True}, 'catalog': {'active': True}}
            if service == 'backend': return binding_status, {'synthetic': True, 'inputBound': binding_status == 200}
            return 202, {'data': {'profileStatus': 'PENDING'}}
        backend.check_live_budget = budget
        backend.request = request
        body = server.ProfileInput(recipientUserId=42, sourceVersion=2, giftPreference='SYNTHETIC')
        try:
            result = await backend.profile(body)
            assert result['httpStatus'] == (202 if binding_status == 200 else binding_status)
            assert calls[2][:3] == ('backend', 'PUT', '/fixture/recipients/42/profile-input')
            assert calls[2][3] == body.model_dump()
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
            await login(http)
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
        rid,tid,cid = (str(uuid.uuid4()) for _ in range(3))
        calls = []
        async def request(*args,**kwargs):
            calls.append(args)
            return 404,{'error':{'code':'NOT_FOUND'}}
        backend.request=request
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1:9000') as http:
            await login(http)
            owner = hashlib.sha256(http.cookies.get('console_admin').encode()).hexdigest()
            app.state.admin.save_chat_admission(owner, backend.target_scope,
                {'fingerprint':'synthetic', 'body':{'request_id':rid,'turn_id':tid,'conversation_id':cid},
                 'readable':True, 'conversationOwned':True})
            response=await http.get('/api/chat/'+tid)
            assert response.status_code == 404
            assert 'data' not in response.json()
            assert calls == [('chat', 'GET', backend.chat_base()+cid+'/turns/'+tid)]
        await backend.http.aclose()
    run(case)
