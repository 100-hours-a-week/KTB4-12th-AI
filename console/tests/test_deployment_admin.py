"""Synthetic injected administrator hash, file priority, and durable login."""
import asyncio
import os

import httpx
import pytest

from console.admin_auth import AdminAuth
from console.server import create_app
from console.tests.auth_support import fixture


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("CONSOLE_", "AI_", "OPENROUTER_", "BE_", "PROFILING_")):
            monkeypatch.delenv(name)


def test_deployment_hash_login_survives_restart_without_hash_file(tmp_path, monkeypatch, caplog):
    path, password = fixture()
    encoded = path.read_text()
    monkeypatch.setenv("CONSOLE_ADMIN_HASH", encoded)
    monkeypatch.setenv("CONSOLE_ADMIN_SESSION_FILE", str(tmp_path / "sessions.sqlite3"))
    async def case():
        previous = None
        for index in range(2):
            app = create_app()
            assert app.state.admin.hash_file is None
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9000",
                                         headers={"Origin": "http://127.0.0.1:9000"}) as client:
                if index == 0:
                    response = await client.post("/api/admin/login", json={"password": password})
                    assert response.status_code == 200
                    previous = client.cookies.get(app.state.admin.cookie)
                else:
                    client.cookies.set(app.state.admin.cookie, previous)
                    response = await client.get("/api/admin/session")
                    assert response.status_code == 200
                assert encoded not in response.text and password not in response.text
            await app.state.backend.http.aclose()
            await app.state.provider_usage.close()
    asyncio.run(case())
    assert encoded not in caplog.text and password not in caplog.text


def test_file_hash_has_priority_and_missing_file_never_falls_back(tmp_path, monkeypatch):
    from fastapi import HTTPException
    path, _ = fixture()
    monkeypatch.setenv("CONSOLE_ADMIN_HASH", "invalid-injected-hash")
    monkeypatch.setenv("CONSOLE_ADMIN_HASH_FILE", str(path))
    assert AdminAuth().credential() == path.read_text()
    monkeypatch.setenv("CONSOLE_ADMIN_HASH", path.read_text())
    monkeypatch.setenv("CONSOLE_ADMIN_HASH_FILE", str(tmp_path / "missing"))
    with pytest.raises(HTTPException) as error: AdminAuth().credential()
    assert error.value.status_code == 503


def test_injected_hash_requires_explicit_private_session_storage(monkeypatch):
    from fastapi import HTTPException
    path, _ = fixture()
    monkeypatch.setenv("CONSOLE_ADMIN_HASH", path.read_text())
    for value in (None, "relative/sessions.sqlite3"):
        if value: monkeypatch.setenv("CONSOLE_ADMIN_SESSION_FILE", value)
        with pytest.raises(HTTPException) as error: AdminAuth().credential()
        assert error.value.status_code == 503
