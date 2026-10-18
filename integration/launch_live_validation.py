"""Explicit live integration over an existing owned, stopped full-catalog QA DB.

Uses existing credentials in memory and the original NUC cumulative account.
No account import/reset, no model fallback, no real preferences or BE changes.
"""

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0,str(ROOT))
if __package__:
    from .live_preflight import (
        validate_credentials,
        validate_profiler_auth,
        validate_resume,
    )
else:
    from live_preflight import (
        validate_credentials,
        validate_profiler_auth,
        validate_resume,
    )


def source_fingerprints():
    return {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in (
            "chat/app",
            "profiling/src/profiling",
            "product-search/search",
            "console",
            "integration",
        )
        for p in (ROOT / folder).rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".js", ".css", ".html"}
        and ".venv" not in p.parts
        and "tests" not in p.parts
        and "__pycache__" not in p.parts
    }


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--owned-qa-root", type=Path, required=True)
    parser.add_argument("--pg-bin", type=Path, required=True)
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--console-port", type=int, default=18800)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Retain the same validation-attempt fence and state",
    )
    parser.add_argument("--max-attempts", type=int, default=6)
    parser.add_argument("--max-reserved-usd", type=float, default=0.20)
    parser.add_argument(
        "--budget-state-file",
        type=Path,
        help="Existing preserved validation fence; never reset",
    )
    args = parser.parse_args()
    if not args.owned_qa_root.resolve().is_relative_to(ROOT.parent):
        raise ValueError("This isolated revision cannot take over an existing external QA/run-2 cluster")
    if args.budget_state_file is not None and not args.budget_state_file.is_file():
        raise ValueError("Preserved validation fence must already exist")
    if not 1 <= args.max_attempts <= 100 or not 0 < args.max_reserved_usd <= 1:
        raise ValueError("Unsupported validation guard")
    if os.environ.get("LIVE_VALIDATION_APPROVED") != "1" or not os.getenv(
        "OPENROUTER_API_KEY"
    ):
        raise ValueError(
            "Existing approved live credentials and explicit validation selection required"
        )
    existing = {}
    with Path("/Users/yh/Code/KTB/final/workspaces/ai-chat-demo/.env").open() as source:
        for raw in source:
            key = raw.split("=", 1)[0].strip()
            if (
                key
                in {
                    "STORE_TOKEN",
                    "AI_SERVICE_TOKEN",
                    "SESSION_SECRET",
                    "QA_ADMIN_PASSWORD",
                    "DEMO_ACCESS_TOKEN",
                }
                and "=" in raw
            ):
                existing[key] = raw.strip().split("=", 1)[1].strip().strip("\"'")
    validate_credentials(existing)
    validate_profiler_auth(
        {
            "PROFILING_TAGGER": "openrouter",
            "PROFILING_TAGGER_REQUIRED": "true",
            "PROFILING_SERVICE_TOKEN": existing["AI_SERVICE_TOKEN"],
        }
    )
    qa = args.owned_qa_root.resolve()
    provenance = json.loads((qa / "provenance.json").read_text())
    database_name = provenance.get("databaseName", "")
    import re
    if not re.fullmatch(r"emmet4930_[a-z0-9_]+", database_name):
        raise ValueError("An explicit separately prepared Emmet4930 QA database binding is required")
    expected_database_identity = None
    if args.resume:
        args.budget_state_file, expected_database_identity = validate_resume(
            args.run_dir,
            qa,
            args.budget_state_file,
            args.max_attempts,
            args.max_reserved_usd,
        )
    else:
        args.run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    runtime = qa / "search-root"
    if (
        not qa.name.startswith("ktb-ai-integration-")
        or not (qa / "pg/PG_VERSION").is_file()
    ):
        raise ValueError("Only the recorded owned QA PostgreSQL root is accepted")
    status = subprocess.run(
        [str(args.pg_bin / "pg_ctl"), "-D", str(qa / "pg"), "status"],
        capture_output=True,
        check=False,
    )
    if status.returncode != 3:
        raise ValueError(
            "Existing DB is running or ownership/state unknown; no change made"
        )
    generation = (runtime / "data/CURRENT").read_text().strip()
    catalog = json.loads(
        (runtime / "data/generations" / generation / "catalog.json").read_text()
    )
    if len(catalog["products"]) != 4231:
        raise ValueError("Full canonical QA catalog not verified")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "en_US.UTF-8",
        "LANG": "en_US.UTF-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HF_HUB_OFFLINE": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
    }
    pg_port = free_port()
    urls = {
        k: "http://127.0.0.1:" + str(free_port())
        for k in ("budget", "chat", "search", "profiling", "backend")
    }
    urls["console"] = "http://127.0.0.1:" + str(args.console_port)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.console_port))
    processes = []
    logs = []
    pg_started = False
    done = False

    def stop(*unused):
        nonlocal done
        done = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def start(name, service, module, variables):
        if name == "profiling":
            validate_profiler_auth(variables)
        log = (args.run_dir / (name + ".log")).open("a")
        logs.append(log)
        proc = subprocess.Popen(
            [
                str(ROOT / service / ".venv/bin/python"),
                "-B",
                "-m",
                "uvicorn",
                module,
                "--host",
                "127.0.0.1",
                "--port",
                urls[name].rsplit(":", 1)[1],
                "--no-access-log",
            ],
            cwd=ROOT if service == "console" else ROOT / service,
            env=env | variables,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        processes.append(proc)

    def wait(service, path, token=None):
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as http:
            for _ in range(100):
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError("An owned service stopped; inspect local logs")
                try:
                    response = http.get(
                        urls[service] + path,
                        headers={"Authorization": "Bearer " + token} if token else {},
                    )
                    if response.status_code == 200:
                        return response.json()
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
        raise RuntimeError("Readiness not verified: " + service)

    try:
        subprocess.run(
            [
                str(args.pg_bin / "pg_ctl"),
                "-D",
                str(qa / "pg"),
                "-l",
                str(args.run_dir / "postgres.log"),
                "-o",
                f"-h 127.0.0.1 -c unix_socket_directories= -p {pg_port}",
                "-w",
                "start",
            ],
            env=env,
            capture_output=True,
            check=True,
            timeout=20,
        )
        pg_started = True
        db = f"postgresql://fixture_owner@127.0.0.1:{pg_port}/{database_name}"
        with psycopg.connect(
            db, options="-c default_transaction_read_only=on"
        ) as connection:
            database_identity = str(
                connection.execute(
                    "SELECT run_identity FROM ai_catalog.integration_marker WHERE id"
                ).fetchone()[0]
            )
            if (
                expected_database_identity is not None
                and database_identity != expected_database_identity
            ):
                raise ValueError(
                    "Resumed database identity differs; no live services started"
                )
            assert (
                connection.execute(
                    "SELECT count(*) FROM ai_catalog.integration_marker"
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM ai_catalog.products WHERE package_id=(SELECT package_id FROM ai_catalog.catalog_versions WHERE is_active)"
                ).fetchone()[0]
                == 4231
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM ai_chat.turns WHERE state->>'status'='PROCESSING'"
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM ai_profile.profile_runs WHERE status='RUNNING'"
                ).fetchone()[0]
                == 0
            )
        start(
            "budget",
            "console",
            "integration.budget_gateway:app",
            {
                "STORE_TOKEN": existing["STORE_TOKEN"],
                "LIVE_VALIDATION_MAX_ATTEMPTS": str(args.max_attempts),
                "LIVE_VALIDATION_MAX_RESERVED_USD": str(args.max_reserved_usd),
                "LIVE_VALIDATION_BUDGET_DB": str(
                    args.budget_state_file
                    or args.run_dir / "validation-attempts.sqlite3"
                ),
            },
        )
        with httpx.Client(timeout=20, trust_env=False) as h:
            for _ in range(100):
                try:
                    response = h.post(
                        urls["budget"] + "/rpc",
                        headers={"Authorization": "Bearer " + existing["STORE_TOKEN"]},
                        json={
                            "method": "budget_summary",
                            "args": {"account": "gift-chat-harness-2026-10-06"},
                        },
                    )
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError(
                    "Original cumulative ledger unavailable; no live services enabled"
                )
            balance = response.json()["result"]
            assert (
                balance["limitUsd"] == 5
                and balance["remainingUsd"] >= 0.20
                and not balance["halted"]
            )
        if not (args.run_dir / "budget-before.json").exists():
            (args.run_dir / "budget-before.json").write_text(
                json.dumps(balance, indent=2) + "\n"
            )
        start(
            "search",
            "product-search",
            "app:app",
            {
                "SEARCH_CATALOG_DATABASE_URL": db.replace(
                    "fixture_owner@", "ai_search_service@"
                ),
                "SEARCH_QA_DATABASE_URL": db.replace(
                    "fixture_owner@", "ai_search_service@"
                ),
                "SEARCH_RUNTIME_ROOT": str(runtime),
                "SEARCH_MEDIA_ROOT": str(args.media_root),
            },
        )
        meta = wait("search", "/v1/metadata")
        assert meta["productCount"] == 4231
        with psycopg.connect(
            db, options="-c default_transaction_read_only=on"
        ) as connection:
            assert str(
                connection.execute(
                    "SELECT id FROM ai_catalog.catalog_versions WHERE is_active"
                ).fetchone()[0]
            ) == str(catalog["ai_catalog_version"])
        recipient = 824215001
        seed = {
            "synthetic": True,
            "recipientUserId": recipient,
            "sourceVersion": 1,
            "giftPreference": "합성 검증용 취향: 보온 텀블러와 따뜻한 차를 즐기고 실용적인 컵을 좋아합니다.",
            "dislikedCategories": [],
            "callback": None,
        }
        private = args.run_dir / "backend-state.json"
        if not private.exists():
            private.write_text(json.dumps(seed, ensure_ascii=False))
        private.chmod(0o600)
        start(
            "backend",
            "console",
            "integration.live_backend:app",
            {
                "LIVE_BACKEND_STATE": str(private),
                "AI_SERVICE_TOKEN": existing["AI_SERVICE_TOKEN"],
                "SEARCH_URL": urls["search"],
            },
        )
        start(
            "profiling",
            "profiling",
            "integration.profiler_runtime:app",
            {
                "PYTHONPATH": os.pathsep.join([str(ROOT),str(ROOT / "profiling/src")]),
                "PROFILING_DATABASE_URL": db.replace(
                    "fixture_owner@", "ai_app@"
                ).replace("postgresql://", "postgresql+psycopg://"),
                "PROFILING_CATALOG_SOURCE": "db",
                "PROFILING_TAGGER": "openrouter",
                "PROFILING_TAGGER_MODEL": "openai/gpt-6-luna",
                "PROFILING_TAGGER_REQUIRED": "true",
                "PROFILING_OPENROUTER_API_KEY": os.environ["OPENROUTER_API_KEY"],
                "PROFILING_BUDGET_URL": urls["budget"],
                "STORE_TOKEN": existing["STORE_TOKEN"],
                "PROFILING_EMBEDDING_DIR": str(runtime),
                "PROFILING_EMBEDDING_REQUIRED": "true",
                "PROFILING_SERVICE_TOKEN": existing["AI_SERVICE_TOKEN"],
                "PROFILING_BACKEND_BASE_URL": urls["backend"],
                "PROFILING_CALLBACK_MAX_ATTEMPTS": "1",
            },
        )
        health = wait("profiling", "/health")
        from integration.profiler_health import model_state
        assert model_state(health)['enabled'] and model_state(health)['required'] and health['embedding']['enabled']
        start(
            "chat",
            "chat",
            "app.main:app",
            existing
            | {
                "CHAT_MODEL": "openai/gpt-6-luna",
                "CHAT_RUNTIME_MODE": "live",
                "OPENROUTER_API_KEY": os.environ["OPENROUTER_API_KEY"],
                "SEARCH_URL": urls["search"],
                "SEARCH_TOKEN": existing["AI_SERVICE_TOKEN"],
                "CHAT_MEDIA_ROOT": str(args.media_root),
                "STORE_MODE": "postgres",
                "DATABASE_URL": db.replace("fixture_owner@", "ai_chat_service@")
                + "?options=-csearch_path%3Dai_chat,public",
                "BUDGET_STORE_URL": urls["budget"],
                "INTERNAL_URL": urls["chat"],
                "COOKIE_SECURE": "false",
            },
        )
        ready = wait("chat", "/readyz", existing["AI_SERVICE_TOKEN"])
        assert ready["mode"] == "live" and ready["model"] == "openai/gpt-6-luna"
        fingerprints = source_fingerprints()
        config = {
            "label": "실제 Luna · 4231상품 · 합성 BE 계약 하네스",
            "liveModel": True,
            "expectedModel": "openai/gpt-6-luna",
            "catalogVersion": str(catalog["ai_catalog_version"]),
            "searchSnapshotId": meta["snapshotId"],
            "sourceManifest": hashlib.sha256(
                json.dumps(fingerprints, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "defaultRecipientUserId": recipient,
            "launcherPid": os.getpid(),
            "targets": {k: urls[k] for k in ("chat", "search", "profiling", "backend")},
            "tokens": {
                k: existing["AI_SERVICE_TOKEN"]
                for k in ("chat", "profiling", "backend")
            },
            "database": db,
        }
        target = args.run_dir / "targets.json"
        target.write_text(json.dumps(config))
        target.chmod(0o600)
        start(
            "console",
            "console",
            "console.server:app",
            {"CONSOLE_TARGET_FILE": str(target)},
        )
        wait("console", "/api/config")
        receipt = {
            "mode": "LIVE",
            "syntheticInputOnly": True,
            "url": urls["console"],
            "urls": urls,
            "launcherPid": os.getpid(),
            "model": "openai/gpt-6-luna",
            "recipientUserId": recipient,
            "sourceVersion": json.loads(private.read_text())["sourceVersion"],
            "catalogVersion": str(catalog["ai_catalog_version"]),
            "searchSnapshotId": meta["snapshotId"],
            "catalogProducts": 4231,
            "existingBudgetAccount": "gift-chat-harness-2026-10-06",
            "maxNewReservationUsd": args.max_reserved_usd,
            "maxNewAttempts": args.max_attempts,
            "databasePort": pg_port,
            "databaseName": database_name,
            "databaseIdentity": database_identity,
            "ownedQA": str(qa),
            "budgetStateFile": str(
                args.budget_state_file or args.run_dir / "validation-attempts.sqlite3"
            ),
            "sourceRoot": str(ROOT),
            "sourceHashes": fingerprints,
        }
        (args.run_dir / "runtime.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
        )
        print(
            json.dumps(
                {
                    k: v
                    for k, v in receipt.items()
                    if k not in {"sourceHashes", "ownedQA", "sourceRoot", "urls"}
                }
            ),
            flush=True,
        )
        while not done:
            if any(p.poll() is not None for p in processes):
                raise RuntimeError(
                    "Live process stopped; no mock replacement is provided"
                )
            time.sleep(0.2)
    finally:
        for proc in reversed(processes):
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=30)
        if pg_started:
            subprocess.run(
                [
                    str(args.pg_bin / "pg_ctl"),
                    "-D",
                    str(qa / "pg"),
                    "-m",
                    "smart",
                    "-w",
                    "stop",
                ],
                env=env,
                capture_output=True,
                check=True,
                timeout=30,
            )
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
