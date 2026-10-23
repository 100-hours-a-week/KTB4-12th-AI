"""Provider credential source. No network, ledger, or service-auth operations.

Managed mode requires a private directory mounted RW by Console and RO by apps.
Injected deployment values supply the initial value only. Once a dashboard
record is written, its persistent marker prevents fallback after loss/corruption.
The whole directory must be mounted: replacing a single bind-mounted file would
leave readers on its old inode. External files are read-only to this module.
"""
import contextvars
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import uuid


ACTIVE_KEY = contextvars.ContextVar("openrouter_request_key", default=None)
KEY_FILE = "openrouter-chat-key.json"
KINDS = {"chat": "OPENROUTER_API_KEY_CHAT", "profiler": "OPENROUTER_API_KEY_PROFILER"}
LEGACY_KINDS = {"chat": "OPENROUTER_API_KEY", "profiler": "PROFILING_OPENROUTER_API_KEY"}


def deployment_source(kind, env):
    """Resolve one service's aliases, retaining file precedence within each name.

    Blank variables are unset placeholders. A configured file remains authoritative:
    missing/empty files never fall back. Distinct alias families must agree.
    """
    def source(name):
        path = (env.get(name + "_FILE") or "").strip() or None
        value = (env.get(name) or "").strip() or None
        if value and path and not Path(path).exists():
            path = None  # ECS injected value replaces an absent local Compose mount.
        return path, None if path else value
    current, legacy = source(KINDS[kind]), source(LEGACY_KINDS[kind])
    selected = current if any(current) else legacy
    return (*selected, legacy if any(current) and any(legacy) else None)


def environment(environ=None):
    value = (os.environ if environ is None else environ).get("AI_ENVIRONMENT", "local")
    if value not in {"local", "develop", "production"}:
        raise KeyUnavailable("CREDENTIAL_ENVIRONMENT_INVALID")
    return value


class KeyUnavailable(ValueError):
    def __init__(self, code="PROVIDER_KEY_UNAVAILABLE"):
        self.code = code
        super().__init__(code)


class KeyConflict(KeyUnavailable):
    pass


@dataclass(frozen=True)
class KeySnapshot:
    value: str = field(repr=False)
    revision: str | None = None
    scope: tuple = ()


def private_directory(path):
    directory = Path(path)
    if not directory.is_absolute() or directory.is_symlink():
        raise KeyUnavailable("PRIVATE_KEY_DIRECTORY_REQUIRED")
    try:
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError()
    except (OSError, ValueError):
        raise KeyUnavailable("PRIVATE_KEY_DIRECTORY_REQUIRED") from None
    return directory


def read_file(path, *, private=False, limit=4096):
    """Open without following a final symlink; never include path/content in errors."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError()
            if private and (info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError()
            data = source.read(limit + 1)
            if len(data) > limit:
                raise ValueError()
        return data.decode("utf-8").strip()
    except FileNotFoundError:
        raise KeyUnavailable("PROVIDER_KEY_MISSING") from None
    except (OSError, UnicodeError, ValueError):
        raise KeyUnavailable() from None


class ProviderKey:
    # ServiceToken reuses this private, atomic file format with a separate file
    # and validator. No credential ever substitutes for a different purpose.
    record_file = KEY_FILE
    record_format = "openrouter-key/2"
    legacy_files = ("openrouter-key.json",)
    legacy_record_file = "openrouter-key.json"
    legacy_record_format = "openrouter-key/1"
    separated_files = ("openrouter-chat-key.json", "openrouter-profiler-key.json")

    def __init__(self, *, kind="chat", managed_directory=None, external_file=None, value=None,
                 env="local", aws_secret_id=None, aws_client=None, layout="separate"):
        if kind not in KINDS:
            raise KeyUnavailable("PROVIDER_KEY_KIND_INVALID")
        if (external_file and value or aws_secret_id and (managed_directory or external_file or value)
                or layout == "legacy-shared" and (external_file or value)):
            raise KeyUnavailable("PROVIDER_KEY_SOURCE_CONFLICT")
        self.kind, self.environment = kind, environment({"AI_ENVIRONMENT": env})
        self.record_file = f"openrouter-{kind}-key.json"
        self.directory = private_directory(managed_directory) if managed_directory else None
        self.external_file = external_file
        self._value = value
        self._deployment_alias = None
        self.aws_secret_id, self.aws_client = aws_secret_id, aws_client
        if layout not in {"separate", "legacy-shared"}:
            raise KeyUnavailable("CREDENTIAL_LAYOUT_INVALID")
        if layout == "legacy-shared" and (not self.directory or env != "local" or aws_secret_id):
            raise KeyUnavailable("LEGACY_LOCAL_MANAGED_DIRECTORY_REQUIRED")
        self.layout = layout
        self.mode = "aws" if aws_secret_id else "managed" if self.directory else "external" if external_file or value else "disabled"
        self.scope = (self.environment, self.purpose, self.layout, str(self.directory or external_file or aws_secret_id or self.mode))
        # Runtime evidence is local to this object/process. Readers never write
        # to the managed credential volume, which remains mounted read-only.
        self._success_lock = threading.Lock()
        self._last_success = None

    @property
    def purpose(self):
        return f"openrouter-{self.kind}"

    @classmethod
    def from_environment(cls, kind="chat", environ=None):
        # The positional mapping form is retained for old local callers only.
        if isinstance(kind, dict):
            environ, kind = kind, "chat"
        env = os.environ if environ is None else environ
        if kind not in KINDS:
            raise KeyUnavailable("PROVIDER_KEY_KIND_INVALID")
        directory = env.get("OPENROUTER_MANAGED_KEY_DIR")
        if env.get("AI_CREDENTIAL_LAYOUT") == "legacy-shared" and any(
                env.get(variable) or env.get(variable + "_FILE") for variable in (*KINDS.values(), *LEGACY_KINDS.values())):
            raise KeyUnavailable("PROVIDER_KEY_SOURCE_CONFLICT")
        path, value, alias = deployment_source(kind, env)
        backend = env.get("AI_CREDENTIAL_BACKEND", "files")
        if backend not in {"files", "aws-secrets-manager"}:
            raise KeyUnavailable("CREDENTIAL_BACKEND_INVALID")
        aws_id = env.get(f"OPENROUTER_{kind.upper()}_SECRET_ARN") if backend == "aws-secrets-manager" else None
        if backend == "aws-secrets-manager" and (not aws_id or environment(env) == "local"):
            raise KeyUnavailable("AWS_CREDENTIAL_CONFIG_REQUIRED")
        if (aws_id and (directory or path or value) or
                (directory or aws_id) and env.get("OPENROUTER_EXTERNAL_KEY_CONFIGURED")):
            raise KeyUnavailable("PROVIDER_KEY_SOURCE_CONFLICT")
        source = cls(kind=kind, env=environment(env), managed_directory=directory,
                   external_file=path, value=None if path else value, aws_secret_id=aws_id,
                   layout=env.get("AI_CREDENTIAL_LAYOUT", "separate"))
        source._deployment_alias = alias
        return source

    def legacy_managed_present(self):
        return bool(self.directory and any((self.directory / name).exists()
                    or (self.directory / name).is_symlink() for name in self.legacy_files))

    @property
    def saved_marker(self):
        return self.directory / ("." + self.record_file + ".saved")

    def external_value(self):
        value = read_file(self.external_file) if self.external_file else self._value
        # Check aliases only if deployment is the active source. A saved
        # dashboard record and deliberate repair never read stale seed files.
        if self._deployment_alias is not None:
            path, alias = self._deployment_alias
            other = read_file(path) if path else alias
            if value != other:
                raise KeyUnavailable("PROVIDER_KEY_SOURCE_CONFLICT")
        if not isinstance(value, str) or not value.strip():
            raise KeyUnavailable("PROVIDER_KEY_MISSING")
        if len(value) > 1024 or not value.isascii() or any(c.isspace() for c in value):
            raise KeyUnavailable()
        return value

    def record(self, value, revision):
        return {"format": self.record_format, "environment": self.environment,
                "purpose": self.purpose, "revision": revision, "key": value}

    def parse_record(self, raw, *, version=None):
        try:
            record = json.loads(raw)
            if set(record) != {"format", "environment", "purpose", "revision", "key"}:
                raise ValueError()
            if (record["format"], record["environment"], record["purpose"]) != (self.record_format, self.environment, self.purpose):
                raise ValueError()
            revision = str(uuid.UUID(record["revision"]))
            if revision != record["revision"] or version is not None and revision != version:
                raise ValueError()
            # AWS Secrets must be preprovisioned with a typed empty version.
            value = self.valid_managed_key(record["key"]) if record["key"] is not None else None
            return value, revision
        except (ValueError, TypeError, KeyError, AttributeError):
            raise KeyUnavailable() from None

    def _stored(self, *, include_deployment=True):
        if self.aws_secret_id:
            from integration.aws_credentials import read_current
            return read_current(self)
        private_directory(self.directory)
        if self.layout == "legacy-shared":
            # Explicit, read-only compatibility with the originally selected
            # common directory. Never fall back after a separated key fails.
            if any((self.directory / name).exists() or (self.directory / name).is_symlink()
                   for name in self.separated_files):
                raise KeyUnavailable("CREDENTIAL_LAYOUT_CONFLICT")
            try:
                record = json.loads(read_file(self.directory / self.legacy_record_file, private=True))
                if set(record) != {"format", "revision", "key"} or record["format"] != self.legacy_record_format:
                    raise ValueError()
                revision = str(uuid.UUID(record["revision"]))
                if revision != record["revision"]:
                    raise ValueError()
                return self.valid_managed_key(record["key"]), revision
            except KeyUnavailable:
                raise
            except (ValueError, TypeError, KeyError, AttributeError):
                raise KeyUnavailable() from None
        try:
            return self.parse_record(read_file(self.directory / self.record_file, private=True))
        except KeyUnavailable as exc:
            if exc.code == "PROVIDER_KEY_MISSING" and self.legacy_managed_present() and not getattr(self, "_confirmed_transition", False):
                raise KeyUnavailable("PROVIDER_KEY_MIGRATION_REQUIRED") from None
            if (include_deployment and exc.code == "PROVIDER_KEY_MISSING" and not self.saved_marker.exists()
                    and not self.saved_marker.is_symlink() and (self.external_file or self._value)):
                return self.external_value(), None
            raise

    def snapshot(self):
        if self.directory or self.aws_secret_id:
            value, revision = self._stored()
            if value is None:
                raise KeyUnavailable("PROVIDER_KEY_MISSING")
            return KeySnapshot(value, revision, self.scope)
        return KeySnapshot(self.external_value(), scope=self.scope)

    @staticmethod
    def valid_managed_key(value):
        if not isinstance(value, str) or not re.fullmatch(r"sk-or-v1-[A-Za-z0-9_-]{20,200}", value):
            raise KeyUnavailable("PROVIDER_KEY_INVALID")
        return value

    def _status(self):
        result = {"mode": self.mode, "configured": False, "revision": None, "state": "missing",
                  "verified": False, "writable": self.mode == "managed" and self.layout != "legacy-shared",
                  "environment": self.environment, "appliesTo": ["chat", "profiler"] if self.layout == "legacy-shared" else [self.kind], "migrationRequired": False,
                  "layout": self.layout}
        snapshot = None
        try:
            if self.directory or self.aws_secret_id:
                value, revision = self._stored()
            else:
                snapshot = self.snapshot()
                value, revision = snapshot.value, snapshot.revision
            result.update(configured=value is not None, revision=revision, state="configured" if value is not None else "missing",
                          source="dashboard" if self.mode == "managed" and revision is not None else "deployment")
            if value is not None:
                snapshot = KeySnapshot(value, revision, self.scope)
        except KeyUnavailable as exc:
            migration = exc.code.endswith("MIGRATION_REQUIRED")
            result.update(state="migration_required" if migration else "missing" if exc.code == "PROVIDER_KEY_MISSING" else "unavailable", migrationRequired=migration)
            if self.mode == "aws":
                result["writable"] = False  # No blind overwrite after an unreadable current version.
        return result, snapshot

    def status(self):
        return self._status()[0]

    @staticmethod
    def identity(snapshot):
        """Private comparison only; never expose the key or its digest in status."""
        return snapshot.scope, snapshot.revision, hashlib.sha256(snapshot.value.encode()).digest()

    def record_success(self, identity):
        """Record a completed model response for the exact credential sent.

        A late response for a replaced key must not hide success for the new key.
        A missing/unreadable current source grants no verification evidence.
        """
        if identity is None:
            return
        with self._success_lock:
            try:
                if identity != self.identity(self.snapshot()):
                    return
            except KeyUnavailable:
                return
            self._last_success = identity, datetime.now(timezone.utc).isoformat()

    def runtime_status(self):
        """The responding process's current key and successful-call evidence."""
        result, snapshot = self._status()
        with self._success_lock:
            success = self._last_success
        current = snapshot is not None and success is not None and success[0] == self.identity(snapshot)
        result["usage"] = {"state": "succeeded" if current else "not-observed",
                           "lastSuccessfulCallAt": success[1] if current else None,
                           "scope": "responding-process"}
        return result

    def save(self, value, expected_revision, *, confirm_migration=False):
        writer = copy.copy(self)
        writer._confirmed_transition = confirm_migration is True
        return writer._save(value, expected_revision)

    def _save(self, value, expected_revision):
        if self.mode != "managed" or self.layout == "legacy-shared":
            raise KeyConflict("PROVIDER_KEY_OPERATOR_MANAGED")
        value = self.valid_managed_key(value)
        if expected_revision is not None:
            try:
                if str(uuid.UUID(expected_revision)) != expected_revision:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise KeyConflict("PROVIDER_KEY_REVISION_REQUIRED") from None
        directory = private_directory(self.directory)
        temporary = None
        replaced = False
        try:
            lockfd = os.open(directory / ".key-write.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(lockfd, "rb+") as lease:
                info = os.fstat(lease.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                    raise KeyUnavailable()
                fcntl.flock(lease, fcntl.LOCK_EX)
                try:
                    _, previous = self._stored(include_deployment=False)
                except KeyUnavailable as exc:
                    if exc.code.endswith("MIGRATION_REQUIRED"):
                        raise KeyConflict(exc.code) from None
                    if exc.code != "PROVIDER_KEY_MISSING":
                        raise
                    previous = None
                if previous != expected_revision:
                    raise KeyConflict("PROVIDER_KEY_CHANGED")
                revision = str(uuid.uuid4())
                record = self.record(value, revision)
                temporary = directory / (".key-" + secrets.token_hex(16) + ".tmp")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as output:
                    output.write(json.dumps(record) + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                # Persist the no-fallback boundary before replacement. Undo
                # only this attempt's new marker on a definite first-save
                # failure, while still holding the writer lock. A crash or an
                # uncertain replacement must keep the fail-closed boundary.
                marker_created = False
                try:
                    if not self.saved_marker.exists():
                        fd = os.open(self.saved_marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        marker_created = True
                        os.close(fd)
                        dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(dirfd)
                        finally:
                            os.close(dirfd)
                    os.replace(temporary, directory / self.record_file)
                except OSError:
                    if marker_created and previous is None:
                        try:
                            self.saved_marker.unlink()
                            dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                            try:
                                os.fsync(dirfd)
                            finally:
                                os.close(dirfd)
                        except OSError:
                            raise KeyUnavailable("PROVIDER_KEY_SAVE_UNCONFIRMED") from None
                    raise
                replaced = True
                dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
                state = self.status()
                if state["revision"] != revision or not state["configured"]:
                    raise KeyUnavailable("PROVIDER_KEY_SAVE_UNCONFIRMED")
                return state
        except OSError:
            raise KeyUnavailable("PROVIDER_KEY_SAVE_UNCONFIRMED" if replaced else "PROVIDER_KEY_SAVE_FAILED") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


class ProviderAuth:
    """Installed via the httpx.Auth subclass in the consumer; no secret repr."""
    def __init__(self, source):
        self.source = source

    def authorize(self, request):
        snapshot = ACTIVE_KEY.get()
        if snapshot is None or snapshot.scope != self.source.scope:
            snapshot = self.source.snapshot()
        request.headers["Authorization"] = "Bearer " + snapshot.value
        # HTTPX extensions stay in-process and are not sent as HTTP headers.
        request.extensions["provider_key_identity"] = self.source.identity(snapshot)
        return request
