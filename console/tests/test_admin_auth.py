import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading

import httpx
import pytest
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from console.admin_auth import AdminAuth, TTL, password_hash, verify_password
from console.server import create_app, masked, session_events
from integration.containers.entrypoint import console_proxy_ips
from integration.provider_key import ProviderKey
from console.tests.auth_support import fixture


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("CONSOLE_ADMIN_", "OPENROUTER_", "PROFILING_OPENROUTER_", "BE_SERVICE_TOKEN_", "CHAT_BE_SERVICE_TOKEN_", "PROFILER_BE_SERVICE_TOKEN_")) or key in {"AI_SERVICE_TOKEN", "PROFILING_SERVICE_TOKEN"}:
            monkeypatch.delenv(key)


def app_for(tmp_path, *, origin=None):
    path, password = fixture()
    target = tmp_path / "admin.hash"
    target.write_bytes(path.read_bytes()); target.chmod(0o600)
    return create_app({"adminAuth": {"passwordHashFile": str(target), **({"origin": origin} if origin else {})}}), password


def run(function):
    asyncio.run(function())


def test_scrypt_unique_salt_strong_parameters_and_wrong_password():
    password = secrets.token_urlsafe(24)
    one, two = password_hash(password), password_hash(password)
    assert one != two and one.startswith("scrypt$131072$8$1$")
    assert verify_password(password, one) and not verify_password(password + "wrong", one)
    assert password not in one


def test_predeployment_cli_without_site_packages_then_login(tmp_path):
    """A synthetic local operator prepares the file before any web server starts."""
    import pty
    import select
    import subprocess
    import sys
    import tempfile
    import time
    root = Path(__file__).resolve().parents[2]
    password = secrets.token_urlsafe(24)
    with tempfile.TemporaryDirectory(prefix="synthetic-predeploy-admin-") as temp:
        target = Path(temp)/"private"/"console-admin.hash"
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, "-S", "-B", "-m", "console.admin_auth", "--output", str(target)],
            cwd=root, stdin=slave, stdout=slave, stderr=slave,
            env={"PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"})
        os.close(slave)
        transcript = b""
        sent = 0
        deadline = time.monotonic()+10
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try: chunk = os.read(master, 8192)
                    except OSError: break
                    if not chunk: break
                    transcript += chunk
                    prompt = ["관리자 비밀번호 (12자 이상): ", "다시 입력: "]
                    if sent < 2 and prompt[sent].encode() in transcript:
                        os.write(master, password.encode()+b"\n")
                        sent += 1
                if process.poll() is not None: break
            assert process.wait(timeout=2) == 0 and sent == 2
        finally:
            if process.poll() is None: process.kill(); process.wait()
            os.close(master)
        assert password.encode() not in transcript and b"scrypt$" not in transcript
        assert target.stat().st_mode & 0o777 == 0o600
        assert target.parent.stat().st_mode & 0o777 == 0o700
        assert verify_password(password, target.read_text().strip())
        async def case():
            app = create_app({"adminAuth": {"passwordHashFile": str(target)}})
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000",
                                         headers={"Origin": "http://127.0.0.1:9000"}) as client:
                assert (await client.get("/api/admin/session")).status_code == 401
                assert (await client.post("/api/admin/login", json={"password": password})).status_code == 200
                assert (await client.get("/api/admin/session")).status_code == 200
                assert (await client.post("/api/admin/setup", json={})).status_code == 403
            await app.state.backend.http.aclose()
        run(case)
    assert not target.exists()


def test_unconfigured_is_closed_and_no_remote_setup_exists(tmp_path):
    async def case():
        app = create_app()
        paths = {route.path for route in app.routes}
        assert not paths.intersection({"/setup", "/register", "/api/admin/setup", "/api/admin/register"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as http:
            assert (await http.get("/")).status_code == 303
            assert (await http.get("/api/snapshot")).status_code == 503
            assert (await http.get("/healthz")).json()["adminConfigured"] is False
            assert (await http.get("/login")).status_code == 200
            for path in ("/api/admin/login", "/api/admin/setup", "/api/admin/register"):
                r = await http.post(path, json={"password": "NEVER_INSTALL_THIS_VALUE"}, headers={"Origin": "http://127.0.0.1:9000"})
                assert r.status_code == 503 and "NEVER_INSTALL" not in r.text
            assert app.state.admin.session_file is None
        await app.state.backend.http.aclose()
    run(case)


def test_every_admin_route_rejects_direct_calls_without_session(tmp_path):
    async def case():
        app, _ = app_for(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as http:
            for route in app.routes:
                if not route.path.startswith("/api/") or route.path == "/api/admin/login":
                    continue
                for method in route.methods:
                    path = route.path
                    for name in ("tid", "ident", "recipient", "version"):
                        path = path.replace("{"+name+"}", "1")
                    r = await http.request(method, path, headers={"Origin": "http://127.0.0.1:9000"})
                    assert r.status_code == 401, (method, path, r.status_code)
            # The former catalog token and BE bearer are not Console sessions.
            for header in ({"X-Catalog-Admin-Token": "synthetic-old-token"}, {"Authorization": "Bearer synthetic-be-token"}):
                assert (await http.get("/api/snapshot", headers=header)).status_code == 401
        await app.state.backend.http.aclose()
    run(case)


def test_login_origin_csrf_cookie_logout_and_replay(tmp_path, caplog):
    async def case():
        origin = "https://admin.example.test"
        app, password = app_for(tmp_path, origin=origin)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=origin) as http:
            body = {"password": password}
            for headers in ({}, {"Origin": "https://evil.test"}, {"Origin": "http://admin.example.test"}):
                assert (await http.post("/api/admin/login", json=body, headers=headers)).status_code == 403
            headers = {"Origin": origin}
            bad = await http.post("/api/admin/login", json={"password": "SYNTHETIC_WRONG_PASSWORD"}, headers=headers)
            assert bad.status_code == 401 and "SYNTHETIC_WRONG" not in bad.text
            good = await http.post("/api/admin/login", json=body, headers=headers)
            assert good.status_code == 200
            cookie = good.headers["set-cookie"]
            assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
            assert f"Max-Age={30*24*60*60}" in cookie
            session_token = http.cookies.get(app.state.admin.cookie)
            assert session_token not in good.text and password not in good.text
            assert (await http.get("/api/config")).status_code == 200
            assert (await http.put("/api/admin/provider-key", json={}, headers=headers)).status_code == 403
            headers["X-CSRF-Token"] = good.json()["csrf"]
            assert (await http.post("/api/admin/logout", headers=headers)).status_code == 200
            http.cookies.set(app.state.admin.cookie, session_token)
            assert (await http.get("/api/config")).status_code == 401
            assert password not in caplog.text and session_token not in caplog.text and good.json()["csrf"] not in caplog.text
            assert (await http.get("/api/config", headers={"Host": "evil.test"})).status_code == 403
        # HTTPS configuration never accepts a direct HTTP request or a spoofed forwarding header.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://admin.example.test") as http:
            assert (await http.get("/login", headers={"X-Forwarded-Proto": "https"})).status_code == 403
        await app.state.backend.http.aclose()
    run(case)


def test_restart_preserves_session_but_absolute_expiry_rotation_and_removal_revoke(tmp_path):
    async def case():
        app, password = app_for(tmp_path)
        time = [1000.0]; app.state.admin.clock = lambda: time[0]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as http:
            async def login():
                r = await http.post("/api/admin/login", json={"password": password}, headers={"Origin": "http://127.0.0.1:9000"})
                assert r.status_code == 200
            await login(); time[0] += TTL+1
            assert (await http.get("/api/snapshot")).status_code == 401
            await login(); token = http.cookies.get(app.state.admin.cookie)
            restarted, _ = app_for(tmp_path)
            restarted.state.admin.clock = lambda: time[0]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(restarted), base_url="http://127.0.0.1:9000", cookies={app.state.admin.cookie: token}) as other:
                assert (await other.get("/api/admin/session")).json()["expiresIn"] == TTL
                time[0] += TTL-1
                assert (await other.get("/api/admin/session")).json()["expiresIn"] == 1
                time[0] += 1
                assert (await other.get("/api/admin/session")).status_code == 401
            await restarted.state.backend.http.aclose()
            await login()
            path = Path(app.state.admin.hash_file)
            path.write_text(password_hash(secrets.token_urlsafe(24)))
            assert (await http.get("/api/snapshot")).status_code == 401
            path.unlink()
            assert (await http.get("/api/snapshot")).status_code == 503
        await app.state.backend.http.aclose()
    run(case)


def test_rate_limit_bounds_hashing_and_error_payload_never_echoes(tmp_path, monkeypatch):
    async def case():
        app, _ = app_for(tmp_path)
        calls = []
        monkeypatch.setattr("console.admin_auth.verify_password", lambda value, encoded: calls.append(True) or False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as http:
            for _ in range(8):
                r = await http.post("/api/admin/login", json={"password": "SYNTHETIC_CANARY"}, headers={"Origin": "http://127.0.0.1:9000"})
                assert r.status_code == 401 and "SYNTHETIC_CANARY" not in r.text
            assert (await http.post("/api/admin/login", json={"password": "bad"}, headers={"Origin": "http://127.0.0.1:9000", "X-Forwarded-For": "different"})).status_code == 429
            assert len(calls) == 8
        await app.state.backend.http.aclose()
    run(case)


@pytest.mark.parametrize("body", [{"password": {"secret": "SYNTHETIC_CANARY"}}, {"password": "x", "extra": "SYNTHETIC_CANARY"}, {"password": "SYNTHETIC_CANARY"*200}])
def test_invalid_login_is_bounded_and_redacted(tmp_path, body):
    async def case():
        app, _ = app_for(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as http:
            r = await http.post("/api/admin/login", json=body, headers={"Origin": "http://127.0.0.1:9000"})
            assert r.status_code in {400, 413} and "SYNTHETIC_CANARY" not in r.text
        await app.state.backend.http.aclose()
    run(case)


def test_secret_fields_masked():
    raw = {k: "SYNTHETIC_CANARY" for k in ("password", "passwordHash", "credential", "csrf", "Cookie", "Set-Cookie", "session", "api_key")}
    assert "SYNTHETIC_CANARY" not in json.dumps(masked(raw))


def test_hash_file_security(tmp_path):
    path, _ = fixture(); target = tmp_path / "admin.hash"
    target.write_bytes(path.read_bytes()); target.chmod(0o644)
    auth = AdminAuth({"passwordHashFile": str(target)})
    with pytest.raises(Exception): auth.credential()
    target.unlink(); target.symlink_to(path)
    with pytest.raises(Exception): auth.credential()


def test_remote_peer_cannot_use_loopback_http_host_or_forwarded_headers(tmp_path):
    async def case():
        app, _ = app_for(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app, client=("192.0.2.12", 12345)), base_url="http://127.0.0.1:9000") as http:
            r = await http.get("/login", headers={"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https"})
            assert r.status_code == 403
        await app.state.backend.http.aclose()
    run(case)


def test_only_explicit_proxy_can_terminate_https(tmp_path, monkeypatch):
    async def case():
        monkeypatch.setenv("CONSOLE_TRUSTED_PROXY_IPS", "192.0.2.10")
        app, password = app_for(tmp_path, origin="https://admin.example.test")
        wrapped = ProxyHeadersMiddleware(app, trusted_hosts=console_proxy_ips())
        for peer, expected in (("192.0.2.10", 200), ("192.0.2.11", 403)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapped, client=(peer, 12345)), base_url="http://admin.example.test") as http:
                r = await http.post("/api/admin/login", json={"password": password}, headers={"Origin": "https://admin.example.test", "X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.12"})
                assert r.status_code == expected
                if expected == 200: assert "Secure" in r.headers["set-cookie"]
        for value in ("*", "192.0.2.0/24", "proxy.example.test"):
            monkeypatch.setenv("CONSOLE_TRUSTED_PROXY_IPS", value)
            with pytest.raises(ValueError): console_proxy_ips()
        await app.state.backend.http.aclose()
    run(case)


def test_key_api_is_write_only_authenticated_csrf_and_restart_durable(tmp_path, monkeypatch, caplog):
    directory = tmp_path / "provider"; directory.mkdir(mode=0o700)
    monkeypatch.setenv("OPENROUTER_MANAGED_KEY_DIR", str(directory))
    async def case():
        app, password = app_for(tmp_path)
        key = "sk-or-v1-" + secrets.token_hex(32)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as http:
            assert (await http.put("/api/admin/provider-key", json={"key": key, "revision": None}, headers={"Origin": str(http.base_url).rstrip('/')})).status_code == 401
            r = await http.post("/api/admin/login", json={"password": password}, headers={"Origin": str(http.base_url).rstrip('/')})
            assert r.status_code == 200
            http.headers.update({"Origin": str(http.base_url).rstrip('/'), "X-CSRF-Token": r.json()["csrf"]})
            initial = (await http.get("/api/admin/provider-key")).json()
            assert initial["configured"] is False and initial["state"] == "missing"
            assert initial["appliesTo"] == ["chat"]
            # Missing model key does not lock the configuration screen.
            assert (await http.get("/")).status_code == 200
            assert (await http.get("/api/config")).status_code == 200
            bad = await http.put("/api/admin/provider-key", json={"key": {"canary": key}, "revision": None})
            assert bad.status_code == 400 and key not in bad.text
            saved = await http.put("/api/admin/provider-key", json={"key": key, "revision": None})
            assert saved.status_code == 200 and key not in saved.text
            assert saved.json()["verified"] is False
            assert saved.json()["appliesTo"] == ["chat"]
            assert ProviderKey(managed_directory=str(directory)).snapshot().value == key
            stale = await http.put("/api/admin/provider-key", json={"key": "sk-or-v1-" + secrets.token_hex(32), "revision": None})
            assert stale.status_code == 409
            for path in ("/api/admin/provider-key", "/api/snapshot", "/api/config", "/"):
                assert key not in (await http.get(path)).text
            assert not app.state.backend.jobs  # Saving never schedules inference or validation.
            restarted, _ = app_for(tmp_path)
            assert restarted.state.provider_key.snapshot().value == key
            async with httpx.AsyncClient(transport=httpx.ASGITransport(restarted), base_url="http://127.0.0.1:9000", cookies=http.cookies) as other:
                assert (await other.get("/api/admin/session")).status_code == 200
            await restarted.state.backend.http.aclose()
        assert key not in caplog.text and password not in caplog.text
        await app.state.backend.http.aclose()
    run(case)


@pytest.mark.parametrize("reason", ["logout", "expiry", "hash_removed"])
def test_idle_sse_closes_after_session_revocation(tmp_path, reason):
    async def case():
        app, password = app_for(tmp_path); auth = app.state.admin
        now = [1000.0]; auth.clock = lambda: now[0]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as http:
            login = await http.post("/api/admin/login", json={"password": password}, headers={"Origin": str(http.base_url).rstrip('/')})
            assert login.status_code == 200
            cookie = http.cookies.get(auth.cookie)
            request = Request({"type": "http", "method": "GET", "path": "/events", "headers": [(b"cookie", (auth.cookie+'='+cookie).encode())]})
            closed = asyncio.Event(); waiting = asyncio.Event()
            async def upstream():
                try:
                    yield "first synthetic event"
                    waiting.set()
                    await asyncio.Event().wait()
                    yield "must not reach browser"
                finally: closed.set()
            stream = session_events(auth, request, upstream(), interval=0.01)
            assert await anext(stream) == "first synthetic event"
            next_event = asyncio.create_task(anext(stream)); await waiting.wait()
            if reason == "logout":
                r = await http.post("/api/admin/logout", headers={"Origin": str(http.base_url).rstrip('/'), "X-CSRF-Token": login.json()["csrf"]})
                assert r.status_code == 200
            elif reason == "expiry": now[0] += TTL + 1
            else: Path(auth.hash_file).unlink()
            with pytest.raises(StopAsyncIteration): await asyncio.wait_for(next_event, timeout=0.5)
            assert closed.is_set()
        await app.state.backend.http.aclose()
    run(case)


def test_invalid_unicode_password_returns_redacted_client_error(tmp_path):
    async def case():
        app, _ = app_for(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000") as http:
            r = await http.post("/api/admin/login", content=json.dumps({"password": "\ud800SYNTHETIC_CANARY"}), headers={"Origin": str(http.base_url).rstrip('/'), "Content-Type": "application/json"})
            assert r.status_code == 400 and "CANARY" not in r.text
        await app.state.backend.http.aclose()
    run(case)


def test_disconnected_login_keeps_memory_intensive_hashing_serial(monkeypatch):
    async def case():
        auth = AdminAuth(); started = threading.Event(); release = threading.Event()
        active = [0]; maximum = [0]; calls = []
        def verify(*args):
            active[0] += 1; maximum[0] = max(maximum[0], active[0]); calls.append(True)
            started.set(); release.wait(2); active[0] -= 1
            return False
        monkeypatch.setattr('console.admin_auth.verify_password', verify)
        first = asyncio.create_task(auth.check_password('synthetic', 'synthetic'))
        assert await asyncio.to_thread(started.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError): await first
        second = asyncio.create_task(auth.check_password('synthetic', 'synthetic'))
        try:
            await asyncio.sleep(0.03)
            assert len(calls) == 1 and not second.done()
        finally: release.set()
        assert await asyncio.wait_for(second, 1) is False
        assert len(calls) == 2 and maximum[0] == 1
    run(case)


def test_managed_backend_token_changes_both_services_and_never_exposes_secret(tmp_path, monkeypatch):
    directory = tmp_path / "backend-token"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv("BE_SERVICE_TOKEN_DIR", str(directory))

    async def case():
        app, password = app_for(tmp_path)
        backend = app.state.backend
        backend.config["tokens"] = {"profiling": "synthetic-profiler-config"}
        token = "synthetic-chat-" + secrets.token_hex(24)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000",
                                     headers={"Origin": "http://127.0.0.1:9000"}) as http:
            response = await http.post("/api/admin/login", json={"password": password})
            http.headers["X-CSRF-Token"] = response.json()["csrf"]
            state = (await http.get("/api/admin/backend-token")).json()
            assert state["appliesTo"] == ["chat", "profiler"] and state["writable"] is True
            saved = await http.put("/api/admin/backend-token", json={"key": token, "revision": None, "confirmShared": False})
            assert saved.status_code == 200 and saved.json()["appliesTo"] == ["chat", "profiler"]
            assert token not in saved.text
            assert backend.auth("chat") == {"Authorization": "Bearer " + token}
            assert backend.auth("profiling") == {"Authorization": "Bearer " + token}
            assert token not in (await http.get("/api/admin/backend-token")).text
            assert not backend.jobs
            # Neither service falls back to old target-file credentials.
            (directory / "be-shared-service-token.json").unlink()
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as unavailable:
                backend.auth("chat")
            assert unavailable.value.status_code == 503
            with pytest.raises(HTTPException): backend.auth("profiling")
        await backend.http.aclose()
    run(case)



@pytest.mark.parametrize("peer", ["172.18.0.1", "192.168.65.1"])
def test_explicit_loopback_publish_origin_allows_nat_peer_and_keeps_origin_checks(tmp_path, peer):
    origin = "http://127.0.0.1:18811"
    app, password = app_for(tmp_path, origin=origin)
    async def case():
        transport = httpx.ASGITransport(app, client=(peer, 41000))
        async with httpx.AsyncClient(transport=transport, base_url=origin) as client:
            response = await client.post("/api/admin/login", json={"password": password}, headers={"Origin": origin})
            assert response.status_code == 200
            cookie = response.headers["set-cookie"]
            assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" not in cookie
            assert (await client.get("/api/admin/session")).status_code == 200
            for headers in ({"Origin": "http://evil.invalid"},
                            {"Origin": origin, "Host": "evil.invalid"},
                            {"Origin": origin, "Sec-Fetch-Site": "cross-site"}):
                assert (await client.post("/api/admin/login", json={"password": password}, headers=headers)).status_code == 403
    run(case)


@pytest.mark.parametrize("origin", ["http://remote.invalid", "http://192.168.65.1:18811", "http://localhost:18811", "http://127.0.0.2:18811", "http://@127.0.0.1:18811", "https://@remote.invalid"])
def test_explicit_http_origin_requires_literal_host_loopback(origin):
    with pytest.raises(ValueError):
        AdminAuth({"origin": origin})
