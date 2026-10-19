"""Dedicated container launcher; fixed application roles and explicit legacy ledger support."""

import hashlib
import ipaddress
import json
import os
import shutil
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROLES = {
    "profiler",
    "chat",
    "search",
    "console",
    "profiler-smoke",
    "chat-smoke",
    "backend",
}


# Normal runs use provider key limits without a central ledger. Existing ledger
# experiments explicitly select `guarded`; their reservations remain fail closed.
BUDGET_MODES = ("guarded", "provider-key-limits")
PINNED_MODEL = "openai/gpt-6-luna"


def budget_mode(*, smoke=False):
    mode = os.environ.get("AI_BUDGET_MODE", "").strip() or "provider-key-limits"
    if mode not in BUDGET_MODES:
        raise ValueError("AI_BUDGET_MODE must be one of: " + ", ".join(BUDGET_MODES))
    if smoke and mode != "guarded":
        raise ValueError("Synthetic images keep the guarded budget")
    return mode


def model_for(name, mode):
    # A configured model is used as is; the guarded ledger fence only approves the pinned model.
    model = os.environ.get(name, "").strip() or PINNED_MODEL
    if mode == "guarded" and model != PINNED_MODEL:
        raise ValueError("The guarded budget fence approves only " + PINNED_MODEL)
    return model


def secret(name, *, required=True):
    # Values are supplied by existing external secret files (local Compose) or, when no file is
    # configured, by the same-named variable the platform injects (ECS Secrets Manager).
    # Never baked into image/env examples. A configured file always wins.
    path = os.environ.get(name + "_FILE")
    if path:
        value = Path(path).read_text().strip()
        if not value:
            raise ValueError("Required secret file empty: " + name)
        return value
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if required:
        raise ValueError("Required secret file or variable missing: " + name)
    return None


def database(value, role):
    parsed = urlsplit(value.replace("postgresql+psycopg://", "postgresql://"))
    expected_database = os.environ.get("AI_DATABASE_NAME", "")
    if not expected_database.startswith("emmet4930_"):
        raise ValueError("AI_DATABASE_NAME must identify the one prepared application database")
    from integration.database_identity import configured_host
    if parsed.hostname != configured_host() or parsed.path != "/" + expected_database:
        raise ValueError("Dedicated separate PostgreSQL service/database required")
    expected = {
        "profiler": "ai_profiler_service",
        "profiler-smoke": "ai_profiler_service",
        "chat": "ai_chat_service",
        "search": "ai_search_service",
        "console": "ai_console_service",
        "chat-smoke": "ai_chat_service",
    }[role]
    if parsed.username != expected:
        raise ValueError("Dedicated least-privilege database role required")
    from integration.database_identity import validate_dsn, audit
    validate_dsn(value, expected_database, expected)
    audit(value, expected_database)
    if role in ("chat", "chat-smoke"):
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["options"] = "-csearch_path=ai_chat,public"
        value = urlunsplit(parsed._replace(query=urlencode(query)))
    return value


def profile_database(value):
    """Read-only preflight; migrations/roles are an operator's separate action."""
    from integration.audit_profile_database import audit
    if not audit(value)['ready']:
        raise ValueError('Profiler requires prepared migration 0011 and app-role privileges; no automatic repair')


def prepare_encoder(root, source):
    root = Path(root)
    source = Path(source)
    if not (source / "models").is_dir():
        raise ValueError("Externally mounted model assets required")
    target = root / "embedding"
    target.mkdir(parents=True, exist_ok=True)
    code = Path(os.environ.get("ENCODER_CODE_ROOT", "/opt/encoder"))
    lock = json.loads((code / "model.lock.json").read_text())
    for item in lock["files"]:
        asset = source / "models" / item["path"]
        if not asset.is_file() or asset.stat().st_size != item["bytes"]:
            raise ValueError("Required model asset missing or wrong size")
        with asset.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != item["sha256"]:
            raise ValueError("Required model asset hash mismatch")
    for name in ["service.mjs", "runtime.mjs", "documents.mjs", "model.lock.json"]:
        destination = target / name
        if destination.exists():
            if not destination.is_file() or destination.read_bytes() != (code / name).read_bytes():
                raise ValueError("Encoder code binding changed; use a separately prepared runtime")
        else:
            shutil.copyfile(code / name, destination)
    for name, destination in [
        ("models", source / "models"),
        ("node_modules", code / "node_modules"),
    ]:
        path = target / name
        if path.is_symlink():
            if path.resolve() != destination.resolve():
                raise ValueError("Encoder mount binding changed")
        elif path.exists():
            raise ValueError("Unexpected pre-existing encoder directory")
        else:
            path.symlink_to(destination, target_is_directory=True)
    if not shutil.which("node"):
        raise ValueError("Node runtime missing")


def configure(role):
    if role not in ROLES:
        raise ValueError("Only dedicated fixed application roles are supported")
    if role == "console" and os.environ.get("CONSOLE_TARGET_FILE"):
        config = json.loads(Path(os.environ["CONSOLE_TARGET_FILE"]).read_text())
        if not isinstance(config, dict):
            raise ValueError("Explicit Console configuration must be an object")
        return None
    smoke = role.endswith("-smoke")
    if smoke and not Path("/workspace/integration/containers/smoke_only").is_file():
        raise ValueError("Synthetic mode is absent from guarded image")
    if (
        role == "profiler-smoke"
        and not Path("/workspace/integration/tests/emmet_fixture_profiler.py").is_file()
    ):
        raise ValueError("Synthetic provider is absent from guarded release image")
    from integration.service_token import shared_token_configured
    managed_auth = role in {"chat", "profiler"} and shared_token_configured()
    internal_token = secret("AI_INTERNAL_SERVICE_TOKEN" if managed_auth else "AI_SERVICE_TOKEN")
    if managed_auth:
        from integration.service_token import ServiceToken
        os.environ["AI_INTERNAL_SERVICE_TOKEN"] = internal_token
        # The independent healthcheck sees the same explicit source, without
        # relying on mutations made only in this launcher's child environment.
        ServiceToken.from_environment(role)
    else:
        os.environ["AI_SERVICE_TOKEN"] = internal_token
    if role == "backend":
        state = json.loads(Path(os.environ["LIVE_BACKEND_STATE"]).read_text())
        if (
            state.get("synthetic") is not True
            or state.get("recipientUserId") != 42
            or type(state.get("sourceVersion")) is not int
            or state["sourceVersion"] < 1
        ):
            raise ValueError("Only the synthetic local BE harness is configured here")
        return None
    dbkey = {
        "profiler": "PROFILING_DATABASE_URL",
        "profiler-smoke": "PROFILING_DATABASE_URL",
        "chat": "DATABASE_URL",
        "chat-smoke": "DATABASE_URL",
        "search": "SEARCH_CATALOG_DATABASE_URL",
        "console": "DATABASE_URL",
    }[role]
    value = database(secret(dbkey), role)
    os.environ[dbkey] = value
    relay = None
    try:
        if role in ("profiler", "profiler-smoke", "chat"):
            mode = budget_mode(smoke=smoke)
            os.environ["AI_BUDGET_MODE"] = mode
            if mode == "guarded":
                from integration.containers.relay import BudgetRelay

                token = secret("STORE_TOKEN")
                # This existing relay serves one approved account/fence.
                if (os.getenv("BUDGET_ACCOUNT", "gift-chat-harness-2026-10-06") != "gift-chat-harness-2026-10-06"
                        or float(os.getenv("BUDGET_EXPECTED_LIMIT_USD", "5")) != 5):
                    raise ValueError("Guarded relay requires its existing account and five-dollar limit")
            from integration.provider_key import ProviderKey
            source = ProviderKey.from_environment("profiler" if role.startswith("profiler") else "chat")
            managed_key = source.mode in {"managed", "aws"}
            key = "" if managed_key else secret("PROFILING_OPENROUTER_API_KEY" if role == "profiler" else "OPENROUTER_API_KEY")
            if smoke and (managed_key or not key.startswith("synthetic-")):
                raise ValueError("Smoke image refuses real provider credentials")
            model = model_for(
                "PROFILING_TAGGER_MODEL" if role.startswith("profiler") else "CHAT_MODEL", mode
            )
            if mode == "guarded":
                os.environ["STORE_TOKEN"] = token
                relay = BudgetRelay.from_environment(token, synthetic=smoke)
            if role.startswith("profiler"):
                if not managed_key:
                    os.environ["PROFILING_OPENROUTER_API_KEY"] = key
            else:
                if not managed_key:
                    os.environ["OPENROUTER_API_KEY"] = key
                os.environ["SEARCH_TOKEN"] = internal_token
                for name in [
                    "SESSION_SECRET",
                    "QA_ADMIN_PASSWORD",
                    "DEMO_ACCESS_TOKEN",
                ]:
                    os.environ[name] = secret(name)
        if role.startswith("profiler"):
            profile_database(value)
        if role == "search":
            runtime = os.environ.get("SEARCH_RUNTIME_ROOT", "/runtime")
            prepare_encoder(runtime, os.environ.get("ASSET_ROOT", "/assets"))
        if relay:
            relay.start()
        if role in ("profiler", "profiler-smoke"):
            os.environ.update(
                PROFILING_SERVICE_TOKEN="" if managed_auth else internal_token,
                # Synthetic and guarded launchers exercise the same catalog API.
                # Profiler retains policy/profile ownership; Search owns vectors.
                PROFILING_CATALOG_SOURCE="search_http",
                PROFILING_SEARCH_BASE_URL="http://127.0.0.1:8000",
                PROFILING_SEARCH_TOKEN=internal_token,
                PROFILING_TAG_SPEC="tag3disliked",
                PROFILING_TAGGER="openrouter",
                PROFILING_TAGGER_MODEL=model,
                PROFILING_EMBEDDING_DIR=os.environ.get(
                    "SEARCH_RUNTIME_ROOT", "/runtime"
                ),
            )
            if relay:
                os.environ["PROFILING_BUDGET_URL"] = relay.url
            else:
                os.environ.pop("PROFILING_BUDGET_URL", None)
        elif role == "chat":
            os.environ.update(
                STORE_MODE="postgres",
                CHAT_RUNTIME_MODE="live",
                CHAT_MODEL=model,
            )
            if relay:
                os.environ["BUDGET_STORE_URL"] = relay.url
            else:
                os.environ.pop("BUDGET_STORE_URL", None)
        if role == "chat-smoke":
            if os.environ.get("OPENROUTER_API_KEY") or os.environ.get(
                "OPENROUTER_API_KEY_FILE"
            ):
                raise ValueError("Free Chat refuses provider credentials")
            os.environ.update(
                STORE_MODE="postgres",
                CHAT_RUNTIME_MODE="mock",
                SEARCH_TOKEN=os.environ["AI_SERVICE_TOKEN"],
            )
            for name in ["SESSION_SECRET", "QA_ADMIN_PASSWORD", "DEMO_ACCESS_TOKEN"]:
                os.environ[name] = secret(name)
        if role == "search":
            os.environ["SEARCH_QA_DATABASE_URL"] = value
            os.environ["SEARCH_CATALOG_DATABASE_URL"] = value
            os.environ["SEARCH_SERVICE_TOKEN"] = os.environ["AI_SERVICE_TOKEN"]
        if role == "console":
            import httpx
            import psycopg

            host = os.environ.get("CONSOLE_SERVICE_HOST", "127.0.0.1")
            if host not in {"127.0.0.1", "mesh"}:
                raise ValueError("Console service host must be loopback or the private Compose mesh")
            targets = {name: f"http://{host}:{port}" for name, port in
                       (("chat", 7860), ("search", 8000), ("profiling", 8001), ("backend", 8002))}
            with httpx.Client(trust_env=False, timeout=4) as http:
                response = http.get(targets["search"] + "/v1/catalog/contract",
                    headers={"Authorization": "Bearer " + os.environ["AI_SERVICE_TOKEN"]})
                response.raise_for_status()
                metadata = response.json()
                version = metadata["catalogVersion"]
            fixture = Path("/workspace/integration/containers/smoke_only").is_file()
            config = {
                "label": "SYNTHETIC container smoke"
                if fixture
                else "Guarded local integration candidate",
                "localFixtureProfilerFree": fixture,
                "liveModel": not fixture,
                "launcherPid": os.getpid(),
                "defaultRecipientUserId": 42,
                "expectedModel": "openai/gpt-6-luna",
                "catalogVersion": version,
                "searchSnapshotId": metadata["snapshotId"],
                "targets": targets,
                "tokens": {
                    "chat": os.environ["AI_SERVICE_TOKEN"],
                    "profiling": os.environ["AI_SERVICE_TOKEN"],
                    "backend": os.environ["AI_SERVICE_TOKEN"],
                },
                "database": value,
            }
            # Guarded apps share loopback; smoke Console reaches the private mesh over backend.
            # Do not import console.server here: its module-level app reads the config
            # at import time. Uvicorn must import it only after this file/env exists.
            path = Path("/run/runtime/targets.json")
            path.write_text(json.dumps(config))
            path.chmod(0o600)
            os.environ["CONSOLE_TARGET_FILE"] = str(path)
        return relay
    except Exception:
        if relay:
            relay.close()
        raise


def console_proxy_ips():
    values = os.getenv("CONSOLE_TRUSTED_PROXY_IPS", "127.0.0.1,::1").split(",")
    try:
        return ",".join(str(ipaddress.ip_address(value.strip())) for value in values if value.strip())
    except ValueError:
        raise ValueError("CONSOLE_TRUSTED_PROXY_IPS requires exact proxy IP addresses") from None


def main():
    import sys

    import uvicorn

    role = sys.argv[1] if len(sys.argv) == 2 else None
    relay = None
    try:
        relay = configure(role)
        modules = {
            "profiler": "integration.profiler_runtime:app",
            "profiler-smoke": "integration.tests.emmet_fixture_profiler:app",
            "chat": "app.main:app",
            "chat-smoke": "app.main:app",
            "backend": "integration.live_backend:app",
            "search": "app:app",
            "console": "console.server:app",
        }
        uvicorn.run(
            modules[role],
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "8000")),
            workers=1,
            access_log=False,
            **({"proxy_headers": True, "forwarded_allow_ips": console_proxy_ips()} if role == "console" else {}),
        )
    finally:
        if relay is not None:
            relay.close()


if __name__ == "__main__":
    main()
