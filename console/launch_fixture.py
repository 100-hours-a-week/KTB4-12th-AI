"""Owned free local fixture: isolated PostgreSQL, stripped keys, TAGGER=none."""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import sys
import time
from pathlib import Path

import httpx
import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def initialize_database(url, python, run):
    """Only the newly created fixture cluster receives synthetic application roles."""
    from integration.migrate_single_server import ROLES
    with psycopg.connect(url) as db:
        for role in [*ROLES.values(), 'ai_console_service']:
            db.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE').format(sql.Identifier(role)))
    run([str(python), '-B', '-m', 'alembic', 'upgrade', '0011'], ROOT/'profiling', {
        'PROFILING_DATABASE_URL': url.replace('postgresql://', 'postgresql+psycopg://'),
        'PROFILING_APP_DB_ROLE': ROLES['profiler'], 'PROFILING_TAGGER': 'none',
        'PYTHONPATH': str(ROOT/'profiling/src'),
    })
    run([str(python), '-B', '-m', 'integration.migrate_single_server', '--database', 'emmet4930_console'], ROOT, {
        'MIGRATION_DATABASE_URL': url, 'INTEGRATION_SYNTHETIC_ONLY': '1', 'PYTHONPATH': str(ROOT),
    })
    with psycopg.connect(url) as db:
        db.execute('CREATE TABLE ai_catalog.integration_marker(id boolean PRIMARY KEY CHECK(id))')
        db.execute('INSERT INTO ai_catalog.integration_marker VALUES(true)')
        db.execute('GRANT USAGE ON SCHEMA ai_runtime,ai_catalog,ai_chat,ai_profile TO ai_console_service')
        db.execute('GRANT SELECT ON ai_runtime.database_identity,ai_catalog.integration_marker,ai_chat.turns,ai_chat.events,ai_chat.task_budget_attempts,ai_profile.profile_runs,ai_catalog.generation_products,ai_catalog.catalog_state TO ai_console_service')


def check_database(url):
    """Resume only the current prepared fixture; never silently rewrite older state."""
    try:
        with psycopg.connect(url, options='-c default_transaction_read_only=on', connect_timeout=3) as db:
            row = db.execute('''SELECT current_database(),database_name,version,
                (SELECT version_num FROM public.alembic_version) FROM ai_runtime.database_identity WHERE singleton''').fetchone()
            if row != ('emmet4930_console', 'emmet4930_console', 'single-server/1', '0011'):
                raise ValueError('Unexpected fixture database version')
            db.execute('SELECT id,turn,note,created FROM ai_chat.turn_reviews LIMIT 0')
            if db.execute('SELECT count(*) FROM ai_catalog.integration_marker').fetchone()[0] != 1:
                raise ValueError('Fixture database marker missing')
    except (psycopg.Error, ValueError):
        raise RuntimeError('Fixture database needs a separately verified migration; existing state was preserved. Use a new state directory for a fresh fixture.') from None


def stop_children(children):
    for process in reversed(children):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                raise RuntimeError('Graceful shutdown incomplete; processes/state retained') from None


def source_hashes():
    paths = [ROOT / "product-search/app.py"]
    for folder in (
        "chat/app",
        "product-search/search",
        "profiling/src/profiling",
        "console",
    ):
        for directory, subdirs, files in os.walk(ROOT / folder):
            subdirs[:] = [
                d for d in subdirs if d not in {".venv", "tests", "__pycache__"}
            ]
            paths.extend(
                Path(directory) / name for name in files if name.endswith(".py")
            )
    return {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
    }


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_idle(db, profiling_url, timeout=30):
    """Persisted RESULT_READY is a retry receipt, not a running worker."""
    deadline = time.monotonic() + timeout
    with (
        psycopg.connect(
            db, options="-c default_transaction_read_only=on", connect_timeout=5
        ) as connection,
        httpx.Client(timeout=3, trust_env=False) as http,
    ):
        while True:
            chat_busy = connection.execute(
                "SELECT count(*) FROM ai_chat.turns WHERE state->>'status'='PROCESSING'"
            ).fetchone()[0]
            connection.rollback()
            response = http.get(profiling_url + "/health")
            response.raise_for_status()
            stats = response.json()["supervisor"]
            if chat_busy == 0 and stats["running"] == 0 and stats["queued"] == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", type=Path, required=True)
    parser.add_argument("--embedding-assets", type=Path, required=True)
    parser.add_argument("--environment-root", type=Path, default=ROOT,
                        help="Existing service .venv directories; application source stays in this checkout")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--console-port", type=int)
    parser.add_argument(
        "--shutdown-wait",
        type=float,
        default=30,
        help="Bounded graceful drain seconds (0.1..300); never force-kill",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="New synthetic state directory; retain for restart",
    )
    parser.add_argument(
        "--resume", action="store_true", help="Resume only this launcher’s marked state"
    )
    args = parser.parse_args()
    from console.admin_auth import AdminAuth
    AdminAuth().credential()  # Fail before starting a DB if the operator hash is missing.
    if not 0.1 <= args.shutdown_wait <= 300:
        parser.error("--shutdown-wait must be between 0.1 and 300 seconds")
    if args.resume and not args.state_dir:
        parser.error("--resume requires --state-dir")
    required = [args.pg_bin / name for name in ("initdb", "pg_ctl")]
    environments = args.environment_root.resolve()
    required += [
        environments / service / ".venv/bin/python"
        for service in ("chat", "product-search", "profiling", "console")
    ]
    required += [args.embedding_assets / name for name in ("models", "node_modules")]
    if any(not path.exists() for path in required):
        parser.error(
            "Prepare existing PostgreSQL tools, four service environments and offline embedding assets first"
        )
    if not shutil.which("node"):
        parser.error("Node is required for the existing offline encoder")
    pg_config = args.pg_bin / "pg_config"
    if not pg_config.exists():
        parser.error("PostgreSQL pg_config is required to check pgvector")
    share = subprocess.check_output([str(pg_config), "--sharedir"], text=True).strip()
    if not (Path(share) / "extension/vector.control").is_file():
        parser.error("Install matching pgvector before creating a fixture DB")
    for service in ("chat", "product-search", "profiling", "console"):
        result = subprocess.run(
            [
                str(environments / service / ".venv/bin/python"),
                "-B",
                "-c",
                "import fastapi,httpx,psycopg,uvicorn",
            ],
            env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode:
            parser.error("Environment dependencies unavailable: " + service)
    model_lock = json.loads(
        (ROOT / "product-search/embedding/model.lock.json").read_text()
    )
    for record in model_lock["files"]:
        path = args.embedding_assets / "models" / record["path"]
        if not path.is_file() or path.stat().st_size != record["bytes"]:
            parser.error("Offline model file missing or wrong size: " + record["path"])
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != record["sha256"]:
            parser.error("Offline model hash mismatch: " + record["path"])
    result = subprocess.run(
        [
            shutil.which("node"),
            "--input-type=module",
            "-e",
            "await import(process.argv[1])",
            (args.embedding_assets / "runtime.mjs").resolve().as_uri(),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        parser.error("Existing Node encoder dependencies unavailable; no DB created")
    if args.console_port and not 1024 <= args.console_port <= 65535:
        parser.error("--console-port must be between 1024 and 65535")
    if args.console_port:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", args.console_port))
            except OSError:
                parser.error("Console port is occupied; choose another loopback port")
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(
            x in k
            for x in [
                "TOKEN",
                "SECRET",
                "PASSWORD",
                "API_KEY",
                "DATABASE",
                "PROFILING",
                "SEARCH_",
                "CHAT_",
                "STORE_",
                "OPENROUTER",
            ]
        )
    }
    env.update(
        PYTHONDONTWRITEBYTECODE="1",
        HF_HUB_OFFLINE="1",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
    )
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT/'profiling/src')])
    env.pop("PYTHONHOME", None)
    scratch = (
        args.state_dir.resolve()
        if args.state_dir
        else Path(tempfile.mkdtemp(prefix="ai-console-free-"))
    )
    if args.state_dir and not args.resume:
        scratch.mkdir(mode=0o700, parents=True, exist_ok=False)
    if not scratch.is_dir() or scratch.is_symlink():
        raise RuntimeError("Existing real directory required")
    scratch.chmod(0o700)
    lease = (scratch / "launcher.lock").open("a")
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("This fixture already has an active launcher") from None
    marker = scratch / "fixture-state.json"
    saved = json.loads(marker.read_text()) if args.resume else None
    if saved and (
        saved.get("format") != "synthetic-console-fixture/1"
        or saved.get("sourceRoot") != str(ROOT)
    ):
        raise RuntimeError("Refusing foreign state directory")
    if args.resume and not (scratch / "pg/PG_VERSION").is_file():
        raise RuntimeError("Marked PostgreSQL state missing")
    logs = []
    children = []
    pg_started = False
    console_ready = False
    done = False

    def stop(*unused):
        nonlocal done
        done = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def run(command, cwd=ROOT, extra=None):
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env | (extra or {}),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Fixture setup failed: " + result.stderr[-700:])
        return result

    def start(name, command, cwd, extra):
        log = (scratch / (name + ".log")).open("w")
        logs.append(log)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env | extra,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        children.append(process)
        return process

    def app(service, module, bind):
        return [
            str(environments / service / ".venv/bin/python"),
            "-B",
            "-m",
            "uvicorn",
            module,
            "--host",
            "127.0.0.1",
            "--port",
            str(bind),
            "--no-access-log",
        ]

    def wait_ready(service, url, path, token):
        with httpx.Client(timeout=3, trust_env=False) as http:
            for _ in range(150):
                if any(process.poll() is not None for process in children):
                    raise RuntimeError('Fixture process stopped during startup: ' + service)
                try:
                    response = http.get(url + path, headers={'Authorization': 'Bearer ' + token})
                    if response.status_code == 200 and (service != 'profiling' or response.json()['catalog']['active']):
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
        raise RuntimeError('Fixture readiness failed: ' + service)

    try:
        db_port = saved["dbPort"] if saved else port()
        data = scratch / "pg"
        if not saved:
            run(
                [
                    str(args.pg_bin / "initdb"),
                    "-D",
                    str(data),
                    "-U",
                    "fixture_owner",
                    "--auth-local=trust",
                    "--auth-host=trust",
                    "--no-locale",
                    "-E",
                    "UTF8",
                ]
            )
        run(
            [
                str(args.pg_bin / "pg_ctl"),
                "-D",
                str(data),
                "-l",
                str(scratch / "pg.log"),
                "-o",
                # Clients use loopback TCP; long checkout paths need no Unix socket.
                f"-h 127.0.0.1 -c unix_socket_directories= -p {db_port}",
                "-w",
                "start",
            ]
        )
        pg_started = True
        bootstrap = f"postgresql://fixture_owner@127.0.0.1:{db_port}/postgres"
        if not saved:
            with psycopg.connect(bootstrap,autocommit=True) as connection:connection.execute("CREATE DATABASE emmet4930_console")
        db = bootstrap.rsplit("/",1)[0]+"/emmet4930_console"
        runtime = scratch / "search-root"
        if not saved:
            initialize_database(db, environments/'profiling/.venv/bin/python', run)
        check_database(db)
        if not saved:
            runtime.mkdir()
            (runtime / "embedding").mkdir()
            for file in (ROOT / "product-search/embedding").iterdir():
                if file.is_file():
                    (runtime / "embedding" / file.name).write_bytes(file.read_bytes())
            for name in ["models", "node_modules"]:
                (runtime / "embedding" / name).symlink_to(
                    (args.embedding_assets / name).resolve(), target_is_directory=True
                )
            run(
                [
                    str(environments / "product-search/.venv/bin/python"),
                    '-B', '-c',
                    'import os; from pathlib import Path; from integration.containers.smoke_init import stage_catalog; '
                    'stage_catalog(Path(os.environ["FIXTURE_RUNTIME"]), os.environ["CATALOG_OWNER_DATABASE_URL"], Path(os.environ["FIXTURE_SOURCE"]))',
                ],
                extra={"CATALOG_OWNER_DATABASE_URL": db.replace('fixture_owner@','ai_catalog_sync@'),
                       "FIXTURE_RUNTIME": str(runtime), "FIXTURE_SOURCE": str(ROOT/'integration/tests/fixture.json'),
                       "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT/'product-search'), str(ROOT/'profiling/src')])},
            )
        urls = {
            key: "http://127.0.0.1:" + str(port())
            for key in ["chat", "search", "profiling", "callback", "console"]
        }
        if saved:
            urls = saved["urls"]
        if args.console_port:
            urls["console"] = "http://127.0.0.1:" + str(args.console_port)
        state = {
            "format": "synthetic-console-fixture/1",
            "sourceRoot": str(ROOT),
            "dbPort": db_port,
            "urls": urls,
        }
        marker.write_text(json.dumps(state))
        marker.chmod(0o600)
        token = "synthetic-console-fixture-service"
        start(
            "callback",
            app(
                "console",
                "console.fixture_callback:app",
                int(urls["callback"].split(":")[-1]),
            ),
            ROOT,
            {"CONSOLE_FIXTURE_CALLBACK_STATE": str(scratch / "callback-state.json")},
        )
        start(
            "search",
            app("product-search", "app:app", int(urls["search"].split(":")[-1])),
            ROOT / "product-search",
            {
                "SEARCH_CATALOG_DATABASE_URL": db.replace(
                    "fixture_owner@", "ai_search_service@"
                ),
                "SEARCH_QA_DATABASE_URL": db.replace(
                    "fixture_owner@", "ai_search_service@"
                ),
                "SEARCH_RUNTIME_ROOT": str(runtime),
                "SEARCH_MEDIA_ROOT": str(ROOT / "integration/tests/media"),
                "SEARCH_SERVICE_TOKEN": token,
            },
        )
        wait_ready('search', urls['search'], '/readyz', token)
        start(
            "profiling",
            app(
                "profiling", "integration.profiler_free_runtime:app", int(urls["profiling"].split(":")[-1])
            ),
            ROOT / "profiling",
            {
                "PROFILING_DATABASE_URL": db.replace(
                    "fixture_owner@", "ai_profiler_service@"
                ).replace("postgresql://", "postgresql+psycopg://"),
                "PROFILING_CATALOG_SOURCE": "search_http",
                "PROFILING_SEARCH_BASE_URL": urls["search"],
                "PROFILING_SEARCH_TOKEN": token,
                "PROFILING_APP_DB_ROLE": "ai_profiler_service",
                "PROFILING_TAGGER": "none",
                "PROFILING_SERVICE_TOKEN": token,
                "PROFILING_BACKEND_BASE_URL": urls["callback"],
                "PROFILING_CALLBACK_MAX_ATTEMPTS": "1",
                "PROFILING_CATALOG_POLL_TTL_S": "0",
                "PYTHONPATH": os.pathsep.join([str(ROOT),str(ROOT / "profiling/src")]),
            },
        )
        start(
            "chat",
            app("chat", "app.main:app", int(urls["chat"].split(":")[-1])),
            ROOT / "chat",
            {
                "AI_SERVICE_TOKEN": token,
                "SESSION_SECRET": "synthetic-console-session",
                "QA_ADMIN_PASSWORD": "synthetic-console-admin",
                "DEMO_ACCESS_TOKEN": "synthetic-console-demo",
                "CHAT_RUNTIME_MODE": "mock",
                "STORE_MODE": "postgres",
                "DATABASE_URL": db.replace("fixture_owner@", "ai_chat_service@")
                + "?options=-csearch_path%3Dai_chat,public",
                "INTERNAL_URL": urls["chat"],
                "COOKIE_SECURE": "false",
                "CHAT_MEDIA_ROOT": str(ROOT / "integration/tests/media"),
            },
        )
        config = {
            "label": "로컬 합성 서비스 · 무료 실행기",
            "targets": {s: urls[s] for s in ["chat", "search", "profiling"]},
            "tokens": {"chat": token, "profiling": token, "search": token},
            "database": db.replace('fixture_owner@','ai_console_service@'),
            "localFixtureProfilerFree": True,
            "launcherPid": os.getpid(),
            "safetyEvidence": "Owned marked synthetic DB; stripped provider keys; explicit TAGGER=none and CHAT_RUNTIME_MODE=mock",
        }
        private = scratch / "targets.json"
        private.write_text(json.dumps(config))
        private.chmod(0o600)
        for service, path in [('chat', '/readyz'), ('profiling', '/health')]:
            wait_ready(service, urls[service], path, token)
        start(
            "console",
            app("console", "console.server:app", int(urls["console"].split(":")[-1])),
            ROOT,
            {"CONSOLE_TARGET_FILE": str(private)},
        )
        with httpx.Client(timeout=3, trust_env=False) as http:
            for _ in range(50):
                try:
                    response = http.get(urls["console"] + "/healthz")
                    if (
                        response.status_code == 200
                        and response.json()["build"]["serverSha256"]
                        == hashlib.sha256(
                            (ROOT / "console/server.py").read_bytes()
                        ).hexdigest()
                    ):
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Console startup/version handshake failed")
        console_ready = True
        args.output.parent.mkdir(parents=True, exist_ok=True)
        public = {
            "url": urls["console"],
            "callbackFixtureURL": urls["callback"],
            "scratch": str(scratch),
            "launcherPid": os.getpid(),
            "paidCalls": 0,
            "mode": "local controlled fixture",
            "targetsConfigPrivate": True,
            "resumed": bool(saved),
            "sourceHashes": source_hashes(),
        }
        args.output.write_text(json.dumps(public, indent=2) + "\n")
        print(
            json.dumps(
                {k: v for k, v in public.items() if k != "sourceHashes"}
                | {"sourceHashesSaved": True}
            ),
            flush=True,
        )
        while True:
            if any(p.poll() is not None for p in children):
                raise RuntimeError(
                    "A fixture process stopped; console must not claim healthy"
                )
            if done:
                children[-1].terminate()
                children[-1].wait(timeout=30)
                try:
                    idle = wait_for_idle(db, urls["profiling"], args.shutdown_wait)
                except (psycopg.Error, httpx.HTTPError, KeyError):
                    idle = False
                if idle:
                    break
                children.pop()
                start(
                    "console",
                    app(
                        "console",
                        "console.server:app",
                        int(urls["console"].split(":")[-1]),
                    ),
                    ROOT,
                    {"CONSOLE_TARGET_FILE": str(private)},
                )
                done = False
                print(
                    "Shutdown refused after bounded drain: active/unknown jobs retained. UI restored. Wait for stored terminal status, then Ctrl+C again; do not force-kill or start a second launcher.",
                    flush=True,
                )
            time.sleep(0.2)
    finally:
        # Close the local UI first, then allow accepted synthetic jobs to finish.
        if console_ready and children and pg_started:
            if len(children) == 5 and children[-1].poll() is None:
                children[-1].terminate()
                children[-1].wait(timeout=30)
            if not wait_for_idle(db, urls["profiling"], args.shutdown_wait):
                raise RuntimeError(
                    "Active/unknown jobs preserved; refusing forced shutdown. Inspect owned fixture logs before another launch."
                )
        # No Console request was admitted before startup completed. A failed
        # readiness probe must not prevent cleanup of this launcher's children.
        stop_children(children)
        if pg_started:
            run(
                [
                    str(args.pg_bin / "pg_ctl"),
                    "-D",
                    str(scratch / "pg"),
                    "-m",
                    "fast",
                    "-w",
                    "stop",
                ]
            )
        for log in logs:
            log.close()
        lease.close()
        print(
            "Only owned fixture processes stopped; files and original sources preserved.",
            flush=True,
        )


if __name__ == "__main__":
    main()
