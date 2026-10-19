"""Isolated synthetic login used by existing Console behavior regressions."""
import atexit
from functools import lru_cache
from pathlib import Path
import secrets
import tempfile

from console.admin_auth import password_hash
from console.server import create_app


@lru_cache
def fixture():
    temporary = tempfile.TemporaryDirectory(prefix="console-auth-test-")
    atexit.register(temporary.cleanup)
    path = Path(temporary.name) / "admin.hash"
    password = secrets.token_urlsafe(24)
    path.write_text(password_hash(password))
    path.chmod(0o600)
    return path, password


def configured_app(config=None):
    path, _ = fixture()
    temporary = tempfile.TemporaryDirectory(prefix="console-session-test-")
    atexit.register(temporary.cleanup)
    return create_app({**(config or {}), "adminAuth": {"passwordHashFile": str(path),
                      "sessionFile": str(Path(temporary.name) / "sessions.sqlite3")}})


async def login(http):
    _, password = fixture()
    r = await http.post("/api/admin/login", json={"password": password}, headers={"Origin": str(http.base_url).rstrip("/")})
    assert r.status_code == 200
    http.headers["X-CSRF-Token"] = r.json()["csrf"]
