"""Single-operator Console login. Registration exists only as a local CLI.

Sessions expire 30 days after login and survive normal restarts in a private
SQLite file. Use one worker. No password, hash, session ID or CSRF is logged.
"""
import argparse
import asyncio
import base64
from collections import deque
from contextlib import contextmanager
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import time
from urllib.parse import urlsplit

# The pre-deployment operator CLI needs only Python's standard library.
# Web-server imports still require FastAPI normally.
if __name__ != "__main__":
    from fastapi import HTTPException
from integration.provider_key import KeyUnavailable, read_file


PREFIX = "scrypt$131072$8$1$"
TTL = 30 * 24 * 60 * 60


def password_hash(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError("Use a password of 12 to 256 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=131072, r=8, p=1, dklen=32, maxmem=256*1024*1024)
    return PREFIX + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def parse_hash(encoded):
    try:
        if not encoded.startswith(PREFIX):
            raise ValueError()
        salt, digest = (base64.b64decode(x, validate=True) for x in encoded[len(PREFIX):].split("$"))
        if len(salt) != 16 or len(digest) != 32:
            raise ValueError()
        return salt, digest
    except (ValueError, TypeError):
        raise ValueError("Invalid administrator hash") from None


def verify_password(password, encoded):
    salt, expected = parse_hash(encoded)
    value = hashlib.scrypt(password.encode(), salt=salt, n=131072, r=8, p=1, dklen=32, maxmem=256*1024*1024)
    return hmac.compare_digest(value, expected)


async def private_json(request, fields, limit=2048, *, optional=()):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "JSON_REQUIRED")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, "REQUEST_TOO_LARGE")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or not set(fields) <= set(body) or not set(body) <= set(fields) | set(optional):
            raise ValueError()
        return body
    except (ValueError, TypeError):
        raise HTTPException(400, "INVALID_REQUEST") from None


def login_client(http, origin, *, password=None):
    """Interactive diagnostics use the same login; never a bearer/session bypass."""
    parsed = urlsplit(origin)
    if (parsed.path or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None
            or not parsed.hostname or (parsed.scheme != 'https' and not
                (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}))):
        raise ValueError('Console login requires HTTPS or a loopback URL')
    if password is None:
        if not sys.stdin.isatty():
            raise ValueError("Console check requires an interactive administrator login")
        password = getpass.getpass("Console administrator password: ")
    response = http.post(origin + "/api/admin/login", json={"password": password}, headers={"Origin": origin})
    if response.status_code != 200:
        raise ValueError("Console administrator login failed")
    return {"Origin": origin, "X-CSRF-Token": response.json()["csrf"]}


class AdminAuth:
    def __init__(self, config=None):
        config = config or {}
        self.hash_file = config.get("passwordHashFile") or os.getenv("CONSOLE_ADMIN_HASH_FILE")
        self.hash_value = os.getenv("CONSOLE_ADMIN_HASH") if not self.hash_file else None
        saved = config.get("sessionFile") or os.getenv("CONSOLE_ADMIN_SESSION_FILE")
        self.session_file = Path(saved) if saved else Path(str(self.hash_file) + ".sessions.sqlite3") if self.hash_file else None
        self.origin = config.get("origin") or os.getenv("CONSOLE_ADMIN_ORIGIN", "")
        self.secure = self.origin.startswith("https://")
        if self.origin:
            parsed = urlsplit(self.origin)
            local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
            if ((parsed.scheme != "https" and not local_http) or not parsed.hostname or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment or parsed.port == 0
                    or self.origin != f"{parsed.scheme}://{parsed.netloc}"):
                raise ValueError("CONSOLE_ADMIN_ORIGIN must be an exact HTTPS or literal loopback HTTP origin")
        self.cookie = "__Host-console_admin" if self.secure else "console_admin"
        self.attempts = deque()
        self.hash_slots = asyncio.Semaphore(1)
        self.clock = time.time

    def credential(self):
        try:
            if self.hash_file:
                if not Path(self.hash_file).is_absolute():
                    raise ValueError()
                value = read_file(self.hash_file, private=True, limit=256)
            else:
                value = self.hash_value
                if (not isinstance(value, str) or len(value) > 256
                        or self.session_file is None or not self.session_file.is_absolute()):
                    raise ValueError()
            parse_hash(value)
            return value
        except (KeyUnavailable, ValueError):
            raise HTTPException(503, "ADMIN_NOT_CONFIGURED") from None

    @contextmanager
    def session_store(self):
        """Private sessions and their request bindings; a broken store grants no access."""
        try:
            path = self.session_file
            if path is None or not path.is_absolute() or path.parent.is_symlink():
                raise ValueError()
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            parent = path.parent.stat()
            if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
                raise ValueError()
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise ValueError()
            db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=2)
            try:
                db.execute("PRAGMA foreign_keys=ON")
                with db:
                    db.execute("CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,credential TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL)")
                    db.execute("""CREATE TABLE IF NOT EXISTS chat_admissions(
                        request_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                        turn_id TEXT UNIQUE NOT NULL, session_hash TEXT NOT NULL
                            REFERENCES sessions(token_hash) ON DELETE CASCADE,
                        target TEXT NOT NULL, fingerprint TEXT NOT NULL, body TEXT NOT NULL,
                        readable INTEGER NOT NULL, conversation_owned INTEGER NOT NULL,
                        receipt TEXT)""")
                    yield db
            finally:
                db.close()
        except (OSError, ValueError, sqlite3.Error):
            raise HTTPException(503, "ADMIN_SESSION_STORE_UNAVAILABLE") from None

    def _chat_session(self, db, key):
        fingerprint = hashlib.sha256(self.credential().encode()).hexdigest()
        db.execute("DELETE FROM sessions WHERE expires<=? OR credential<>?", (self.clock(), fingerprint))
        if db.execute("SELECT 1 FROM sessions WHERE token_hash=?", (key,)).fetchone() is None:
            raise HTTPException(401, "ADMIN_LOGIN_REQUIRED")

    def chat_admission(self, key, target, *, request_id=None, turn_id=None):
        """Read only an admission recorded by this login for this exact target."""
        column, ident = ("request_id", request_id) if request_id is not None else ("turn_id", turn_id)
        with self.session_store() as db:
            self._chat_session(db, key)
            row = db.execute(f"SELECT session_hash,target,fingerprint,body,readable,conversation_owned,receipt FROM chat_admissions WHERE {column}=?", (ident,)).fetchone()
            if row is None:
                return None
            if row[0] != key or row[1] != target:
                raise HTTPException(409 if request_id is not None else 404, "CONSOLE_CHAT_OWNERSHIP_MISMATCH")
            return {"fingerprint": row[2], "body": json.loads(row[3]), "readable": bool(row[4]),
                    "conversationOwned": bool(row[5]), "receipt": json.loads(row[6]) if row[6] else None}

    def save_chat_admission(self, key, target, row):
        """Durably freeze ownership and the upstream body before a possible send."""
        body = row["body"]
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > 1_048_576:
            raise HTTPException(413, "CONSOLE_CHAT_BODY_TOO_LARGE")
        rid, cid, tid = (body[name] for name in ("request_id", "conversation_id", "turn_id"))
        with self.session_store() as db:
            self._chat_session(db, key)
            bindings = db.execute("SELECT request_id,conversation_id,turn_id,session_hash,target FROM chat_admissions WHERE request_id=? OR turn_id=? OR conversation_id=?", (rid, tid, cid)).fetchall()
            for existing in bindings:
                if (existing[3] != key or existing[4] != target
                        or (existing[0] == rid or existing[2] == tid) and existing[:3] != (rid, cid, tid)):
                    raise HTTPException(409, "CONSOLE_CHAT_OWNERSHIP_MISMATCH")
            previous = db.execute("SELECT fingerprint,body,readable,conversation_owned FROM chat_admissions WHERE request_id=?", (rid,)).fetchone()
            if previous is not None and previous[:2] != (row["fingerprint"], encoded):
                raise HTTPException(409, "CONSOLE_CHAT_BODY_CHANGED")
            if previous is None and db.execute("SELECT count(*) FROM chat_admissions WHERE session_hash=?", (key,)).fetchone()[0] >= 256:
                raise HTTPException(429, "CONSOLE_CHAT_HISTORY_LIMIT")
            readable = bool(row.get("readable"))
            owned = bool(row.get("conversationOwned") or previous and previous[3])
            receipt = json.dumps(row["receipt"], separators=(",", ":")) if row.get("receipt") is not None else None
            db.execute("""INSERT INTO chat_admissions VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(request_id) DO UPDATE SET readable=excluded.readable,
                    conversation_owned=excluded.conversation_owned,receipt=excluded.receipt""",
                (rid, cid, tid, key, target, row["fingerprint"], encoded, readable, owned, receipt))

    def owns_chat_conversation(self, key, target, cid):
        with self.session_store() as db:
            self._chat_session(db, key)
            rows = db.execute("SELECT session_hash,target,conversation_owned FROM chat_admissions WHERE conversation_id=?", (cid,)).fetchall()
            if any(row[:2] != (key, target) for row in rows):
                raise HTTPException(409, "CONSOLE_CHAT_OWNERSHIP_MISMATCH")
            return any(row[2] for row in rows)

    def check_origin(self, request):
        host = request.headers.get("host", "")
        expected = self.origin or "http://" + host
        if self.origin:
            # An explicit loopback origin supports a port published only on the
            # host loopback interface, where Docker presents a gateway peer.
            parsed = urlsplit(self.origin)
            valid = host == parsed.netloc and request.url.scheme == parsed.scheme
        else:
            try:
                parsed = urlsplit(expected)
                valid = (request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
                         and request.url.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                         and parsed.netloc == host and parsed.username is None and parsed.password is None)
                parsed.port
            except ValueError:
                valid = False
        if not valid or request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "ORIGIN_REJECTED")
        origin = request.headers.get("origin")
        if (origin is not None and origin != expected) or (request.method not in {"GET", "HEAD"} and origin != expected):
            raise HTTPException(403, "ORIGIN_REJECTED")

    def throttle(self, request):
        now = self.clock()
        while self.attempts and self.attempts[0][0] <= now-300:
            self.attempts.popleft()
        peer = request.client.host if request.client else "unknown"
        if len(self.attempts) >= 32 or sum(address == peer for _, address in self.attempts) >= 8:
            raise HTTPException(429, "LOGIN_RATE_LIMITED", headers={"Retry-After": "300"})
        self.attempts.append((now, peer))

    def session(self, request, *, csrf=True):
        credential = self.credential()
        now = self.clock()
        token = request.cookies.get(self.cookie, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise HTTPException(401, "ADMIN_LOGIN_REQUIRED")
        key = hashlib.sha256(token.encode()).hexdigest()
        fingerprint = hashlib.sha256(credential.encode()).hexdigest()
        with self.session_store() as db:
            db.execute("DELETE FROM sessions WHERE expires<=? OR credential<>?", (now, fingerprint))
            row = db.execute("SELECT csrf,expires,created FROM sessions WHERE token_hash=? AND credential=?", (key, fingerprint)).fetchone()
        if row is None:
            raise HTTPException(401, "ADMIN_LOGIN_REQUIRED")
        session = dict(zip(("csrf", "expires", "created"), row))
        if csrf and request.method not in {"GET", "HEAD"}:
            supplied = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(supplied.encode(), session["csrf"].encode()):
                raise HTTPException(403, "CSRF_REJECTED")
        return key, session

    async def check_password(self, password, credential):
        # Cancelling an HTTP request does not stop scrypt's worker thread. Hold
        # the slot until that thread finishes, including disconnected clients.
        await self.hash_slots.acquire()
        task = asyncio.create_task(asyncio.to_thread(verify_password, password, credential))
        def finished(result):
            self.hash_slots.release()
            if not result.cancelled(): result.exception()
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def login(self, request, response):
        self.throttle(request)
        credential = self.credential()
        body = await private_json(request, {"password"})
        password = body["password"]
        if not isinstance(password, str) or not 1 <= len(password) <= 256:
            raise HTTPException(400, "INVALID_REQUEST")
        try:
            valid = await self.check_password(password, credential)
        except UnicodeError:
            raise HTTPException(400, "INVALID_REQUEST") from None
        if not valid:
            raise HTTPException(401, "LOGIN_FAILED")
        # Re-read after the expensive check; rotation/removal wins over an old login.
        if not hmac.compare_digest(credential, self.credential()):
            raise HTTPException(401, "LOGIN_FAILED")
        now = self.clock()
        old = request.cookies.get(self.cookie, "")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        fingerprint = hashlib.sha256(credential.encode()).hexdigest()
        with self.session_store() as db:
            db.execute("DELETE FROM sessions WHERE expires<=? OR credential<>? OR token_hash=?",
                       (now, fingerprint, hashlib.sha256(old.encode()).hexdigest()))
            if db.execute("SELECT count(*) FROM sessions").fetchone()[0] >= 32:
                raise HTTPException(429, "SESSION_LIMIT_REACHED")
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), csrf, fingerprint, now, now + TTL))
        response.set_cookie(self.cookie, token, max_age=TTL, path="/", secure=self.secure, httponly=True, samesite="strict")
        return {"authenticated": True, "csrf": csrf, "expiresIn": TTL}

    def logout(self, request, response):
        key, _ = self.session(request)
        with self.session_store() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (key,))
        response.delete_cookie(self.cookie, path="/", secure=self.secure, httponly=True, samesite="strict")
        return {"authenticated": False}


def main():
    parser = argparse.ArgumentParser(description="Create an administrator password hash locally; never print the password/hash.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # No CLI password/stdin option: prevent shell history and remote setup takeover.
    if not sys.stdin.isatty():
        parser.error("An interactive operator terminal is required")
    target = args.output
    source = Path(__file__).resolve().parents[1]
    if not target.is_absolute() or target.resolve().is_relative_to(source) or target.exists() or target.is_symlink():
        parser.error("Choose a new absolute path outside the checkout")
    password = getpass.getpass("관리자 비밀번호 (12자 이상): ")
    if password != getpass.getpass("다시 입력: "):
        parser.error("Passwords do not match")
    try:
        encoded = password_hash(password)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(encoded + "\n")
            output.flush()
            os.fsync(output.fileno())
    except (OSError, ValueError):
        parser.error("Could not create the private hash file")
    print("Private hash file created. Give it to the deployment operator before startup; keep it out of Git and images. No server access is required to create it.")


if __name__ == "__main__":
    main()
