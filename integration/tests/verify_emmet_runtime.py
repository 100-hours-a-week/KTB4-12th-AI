"""Temporary native processes over the separately owned QA cluster; provider wire always mocked."""

import argparse
import json, os, sys, time, socket, subprocess
from pathlib import Path
import httpx, psycopg, sqlalchemy as sa
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT.parent / "verification"
PG = Path("/opt/homebrew/opt/postgresql@18/bin")
sys.path[:0] = [str(ROOT), str(ROOT / "profiling/src"), str(ROOT / "product-search")]
from integration.profiler_generation import BoundGeneration
from integration.catalog_transition import upgrade_0008


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    global BASE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-root", type=Path, default=BASE)
    args = parser.parse_args()
    BASE = args.qa_root.resolve()
    if not BASE.is_relative_to(ROOT.parent):
        raise ValueError("Use only the separately owned integration QA workspace")
    cluster = json.loads((BASE / "cluster.json").read_text())
    assert cluster["root"] == str(BASE) and cluster["qaOnly"] is True
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(
            w in k
            for w in [
                "TOKEN",
                "SECRET",
                "PASSWORD",
                "API_KEY",
                "DATABASE",
                "PROFILING_",
                "CHAT_",
                "SEARCH_",
            ]
        )
    }
    env.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT / "profiling/src")]),
        HF_HUB_OFFLINE="1",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
    )

    def run(cmd):
        p = subprocess.run(
            list(map(str, cmd)), env=env, capture_output=True, text=True, timeout=90
        )
        if p.returncode:
            raise RuntimeError(p.stderr[-1500:])

    def start(name, module, python, variables):
        service_port = port()
        if module == "app.main:app":
            variables = variables | {"INTERNAL_URL": f"http://127.0.0.1:{service_port}"}
        log = (BASE / (name + ".log")).open("w")
        logs.append(log)
        proc = subprocess.Popen(
            [
                str(python),
                "-B",
                "-m",
                "uvicorn",
                module,
                "--host",
                "127.0.0.1",
                "--port",
                str(service_port),
                "--no-access-log",
            ],
            cwd=ROOT,
            env=env | variables,
            stdout=log,
            stderr=log,
        )
        children.append(proc)
        return f"http://127.0.0.1:{service_port}"

    def wait(path):
        until = time.monotonic() + 90
        while time.monotonic() < until:
            if any(c.poll() is not None for c in children):
                raise RuntimeError(
                    "A temporary fixture process exited; inspect owned fixture logs"
                )
            try:
                r = http.get(path)
                if r.status_code == 200:
                    return r.json()
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise TimeoutError("Temporary fixture readiness timeout")

    children = []
    logs = []
    started = False
    owner = None
    result = {
        "synthetic": True,
        "sourceScope": "emmet4930 isolated native QA; fake ledger/provider; real PostgreSQL/Node/Search/Profiler/Console",
        "paidCalls": 0,
        "realLedgerCalls": 0,
        "existingRuntimeTouched": False,
        "checks": [],
    }
    try:
        run(
            [
                PG / "pg_ctl",
                "-D",
                BASE / "pg",
                "-l",
                BASE / "postgres.log",
                "-o",
                f"-h 127.0.0.1 -c unix_socket_directories= -p {cluster['port']}",
                "-w",
                "start",
            ]
        )
        started = True
        runtime_name = "emmet4930_runtime_" + str(time.time_ns())
        with psycopg.connect(cluster["baseUrl"], autocommit=True) as db:
            db.execute(
                "CREATE DATABASE " + runtime_name + " TEMPLATE emmet4930_upgrade"
            )
        dburl = cluster["baseUrl"].rsplit("/", 1)[0] + "/" + runtime_name
        runtime = BASE / "emmet4930_upgrade/search-root"
        mode = BASE / "provider-mode.json"
        mode.write_text(json.dumps({"mode": "success"}))
        owner = sa.create_engine(
            dburl.replace("postgresql://", "postgresql+psycopg://")
        )
        binding = BoundGeneration(runtime, owner)
        assert binding.count == 4231
        result["checks"].append(
            "DB version/canonical JSON/vectors/encoder fingerprint and full4231 row binding"
        )
        negative_name = "emmet4930_negative_" + str(time.time_ns())
        with psycopg.connect(cluster["baseUrl"], autocommit=True) as db:
            db.execute("CREATE DATABASE " + negative_name)
        negative = cluster["baseUrl"].rsplit("/", 1)[0] + "/" + negative_name
        p = subprocess.run(
            [
                str(ROOT / "profiling/.venv/bin/python"),
                "-B",
                "-m",
                "alembic",
                "upgrade",
                "0007",
            ],
            cwd=ROOT / "profiling",
            env=env
            | {
                "PROFILING_DATABASE_URL": negative.replace(
                    "postgresql://", "postgresql+psycopg://"
                )
            },
            capture_output=True,
            text=True,
        )
        assert p.returncode == 0, p.stderr
        neg = sa.create_engine(
            negative.replace("postgresql://", "postgresql+psycopg://")
        )
        with neg.begin() as db:
            db.exec_driver_sql(
                "CREATE TABLE ai_catalog.unexpected_reference(source_id text REFERENCES ai_catalog.products(source_product_id))"
            )
        try:
            upgrade_0008(neg)
        except ValueError:
            pass
        else:
            raise AssertionError("Unknown dependency must refuse transition")
        with neg.begin() as db:
            assert (
                db.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar_one()
                == "0007"
            )
            assert (
                db.exec_driver_sql(
                    "SELECT to_regclass('ai_catalog.integration_category_sources')"
                ).scalar_one()
                is None
            )
            db.exec_driver_sql("DROP TABLE ai_catalog.unexpected_reference")
            db.exec_driver_sql(
                "INSERT INTO ai_catalog.categories(source_category_id,name,level,product_count,taxonomy_version) VALUES('SYNTHETIC','SYNTHETIC',1,0,'SYNTHETIC')"
            )
        try:
            upgrade_0008(neg)
        except ValueError:
            pass
        else:
            raise AssertionError("Missing Backend ID must refuse transition")
        neg.dispose()
        result["checks"].append(
            "unknown FK and missing BE IDs refuse atomically before migration"
        )
        with httpx.Client(trust_env=False, timeout=15) as http:
            python = ROOT / "profiling/.venv/bin/python"
            budget = start(
                "fixture-budget",
                "integration.tests.emmet_fixture_services:app",
                python,
                {"EMMET_FIXTURE_MODE": str(mode)},
            )
            wait(budget + "/fixture/ledger")
            search = start(
                "fixture-search",
                "app:app",
                ROOT / "product-search/.venv/bin/python",
                {
                    "PYTHONPATH": str(ROOT / "product-search"),
                    "SEARCH_CATALOG_DATABASE_URL": dburl.replace(
                        "fixture_owner@", "ai_search_service@"
                    ),
                    "SEARCH_QA_DATABASE_URL": dburl.replace(
                        "fixture_owner@", "ai_search_service@"
                    ),
                    "SEARCH_RUNTIME_ROOT": str(runtime),
                    "SEARCH_MEDIA_ROOT": str(BASE / "emmet4930_fresh/media"),
                },
            )
            metadata = wait(search + "/v1/metadata")
            assert metadata["productCount"] == 4231
            state = BASE / "synthetic-backend-state.json"
            state.write_text(
                json.dumps(
                    {
                        "synthetic": True,
                        "recipientUserId": 42,
                        "sourceVersion": 1,
                        "giftPreference": "SYNTHETIC",
                        "dislikedCategories": [],
                        "callback": None,
                    }
                )
            )
            state.chmod(0o600)
            backend = start(
                "fixture-backend",
                "integration.live_backend:app",
                ROOT / "console/.venv/bin/python",
                {
                    "LIVE_BACKEND_STATE": str(state),
                    "AI_SERVICE_TOKEN": "synthetic-token",
                    "SEARCH_URL": search,
                },
            )
            # auth-protected context proves the fixture receiver is up, without starting a job.
            for _ in range(100):
                try:
                    if (
                        http.get(
                            backend + "/fixture/recipients/42/chat-context",
                            headers={"Authorization": "Bearer synthetic-token"},
                        ).status_code
                        == 200
                    ):
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            profiler = start(
                "fixture-profiler",
                "integration.tests.emmet_fixture_profiler:app",
                python,
                {
                    "EMMET_FIXTURE_MODE": str(mode),
                    "PROFILING_DATABASE_URL": dburl.replace(
                        "fixture_owner@", "ai_app@"
                    ).replace("postgresql://", "postgresql+psycopg://"),
                    "PROFILING_CATALOG_SOURCE": "db",
                    "PROFILING_CATALOG_POLL_TTL_S": "0",
                    "PROFILING_TAGGER": "openrouter",
                    "PROFILING_TAGGER_MODEL": "openai/gpt-6-luna",
                    "PROFILING_OPENROUTER_API_KEY": "synthetic-not-a-real-key",
                    "PROFILING_EMBEDDING_DIR": str(runtime),
                    "PROFILING_SERVICE_TOKEN": "synthetic-token",
                    "PROFILING_BACKEND_BASE_URL": backend,
                    "PROFILING_BUDGET_URL": budget,
                    "AI_BUDGET_MODE": "guarded",
                    "STORE_TOKEN": "synthetic-token",
                    "PROFILING_CALLBACK_MAX_ATTEMPTS": "1",
                },
            )
            health = wait(profiler + "/health")
            assert (
                health["llm"]["enabled"]
                and health["integration"]["budgetGuard"] == "central-account"
                and health["store"]["migration"] == "0008"
            )
            assert http.get(profiler + "/readyz").status_code == 200
            result["checks"].append(
                "actual guarded app startup: mandatory Node warmup, llm health contract, exact0008 and readyz"
            )
            auth = {"Authorization": "Bearer synthetic-token"}
            statuses = []
            for version, case in enumerate(
                ["success", "error", "bad_json", "timeout", "missing_usage"], 1
            ):
                mode.write_text(json.dumps({"mode": case}))
                body = {
                    "recipientUserId": 42,
                    "sourceVersion": version,
                    "giftPreference": "합성 검증용 취향: 실용적인 컵을 좋아합니다.",
                    "dislikedCategories": [],
                    "reviews": [],
                }
                assert (
                    http.put(
                        backend + "/fixture/recipients/42/profile-input",
                        json=body,
                        headers=auth,
                    ).status_code
                    == 200
                )
                response = http.post(
                    profiler + "/api/internal/v1/ai/profile/extract-and-pool",
                    json=body,
                    headers=auth,
                )
                assert response.status_code == 202, response.status_code
                until = time.monotonic() + 45
                while time.monotonic() < until:
                    with psycopg.connect(
                        dburl, options="-c default_transaction_read_only=on"
                    ) as db:
                        row = db.execute(
                            "SELECT status,tag_status,callback_attempts,error->>'code' error_code,llm_cost_usd FROM ai_profile.profile_runs WHERE recipient_user_id=42 AND source_version=%s",
                            (version,),
                        ).fetchone()
                    if row and row[0] in ["DELIVERED", "FAILED"]:
                        break
                    time.sleep(0.1)
                else:
                    raise TimeoutError("Synthetic profile completion timeout")
                expected = (
                    "DELIVERED" if case in ["success", "missing_usage"] else "FAILED"
                )
                assert row[0] == expected, (case, row)
                statuses.append(
                    {
                        "case": case,
                        "status": row[0],
                        "tagStatus": row[1],
                        "callbackAttempts": row[2],
                        "errorCode": row[3],
                        "storedCostUsd": float(row[4]) if row[4] is not None else None,
                    }
                )
                if expected == "FAILED":
                    assert row[2] == 0
                    assert json.loads(state.read_text())["sourceVersion"] == 1
            ledger = http.get(budget + "/fixture/ledger").json()
            assert len(ledger["attempts"]) == 5
            assert sum(a["status"] == "received" for a in ledger["attempts"]) == 1
            assert sum(a["status"] == "unknown" for a in ledger["attempts"]) == 4
            result["checks"].append(
                "actual pipeline: reserve/settle success; HTTP error, malformed JSON, timeout fail without callback; unknown usage retains reservation"
            )
            result["profileCases"] = statuses
            result["syntheticLedger"] = ledger
            no_signal = []
            first_id = json.loads(
                (
                    runtime
                    / "data/generations"
                    / (runtime / "data/CURRENT").read_text().strip()
                    / "manifest.json"
                ).read_text()
            )["product_ids"][0]
            for version, case in [(6, "low_rating"), (7, "filtered_preference")]:
                no_signal_body = {
                    "recipientUserId": 42,
                    "sourceVersion": version,
                    "giftPreference": "ㅋㅋㅋㅋㅋㅋ"
                    if case == "filtered_preference"
                    else None,
                    "dislikedCategories": [],
                    "reviews": [
                        {
                            "productId": first_id,
                            "rating": 2,
                            "reviewText": "합성 낮은 평점 후기",
                        }
                    ]
                    if case == "low_rating"
                    else [],
                }
                assert (
                    http.put(
                        backend + "/fixture/recipients/42/profile-input",
                        json=no_signal_body,
                        headers=auth,
                    ).status_code
                    == 200
                )
                assert (
                    http.post(
                        profiler + "/api/internal/v1/ai/profile/extract-and-pool",
                        json=no_signal_body,
                        headers=auth,
                    ).status_code
                    == 202
                )
                until = time.monotonic() + 30
                while time.monotonic() < until:
                    with psycopg.connect(
                        dburl, options="-c default_transaction_read_only=on"
                    ) as db:
                        row = db.execute(
                            "SELECT status,tag_status,callback_attempts FROM ai_profile.profile_runs WHERE recipient_user_id=42 AND source_version=%s",
                            (version,),
                        ).fetchone()
                        persisted = db.execute(
                            "SELECT source_version,tag_status FROM ai_profile.recipient_profiles WHERE recipient_user_id=42"
                        ).fetchone()
                    if row and row[0] in ("DELIVERED", "FAILED"):
                        break
                    time.sleep(0.1)
                assert row == ("DELIVERED", "skip_no_signal", 1), (case, row)
                assert persisted == (version, "skip_no_signal")
                assert http.get(budget + "/fixture/ledger").json() == ledger
                assert json.loads(state.read_text())["sourceVersion"] == version
                no_signal.append(
                    {
                        "case": case,
                        "status": row[0],
                        "tagStatus": row[1],
                        "modelCalls": 0,
                        "budgetAttemptsAdded": 0,
                        "recipientPersisted": True,
                        "callbackAttempts": row[2],
                    }
                )
            result["noSignalCases"] = no_signal
            result["checks"].append(
                "legitimate skip_no_signal preserves profile and callback with zero model/reservation calls"
            )
            previous = http.get(
                backend + "/fixture/recipients/42/chat-context", headers=auth
            ).json()
            assert (
                previous["recipientContext"]["sourceVersion"] == 7
                and len(previous["preparedContext"]["productIds"]) == 30
            )
            # Corrupt only the owned QA DB generation, and restore it afterward. Readiness/admission must fail before provider work.
            with psycopg.connect(dburl) as db:
                original = db.execute(
                    "SELECT manifest FROM ai_catalog.search_generations"
                ).fetchone()[0]
                bad = original | {
                    "product_ids": list(reversed(original["product_ids"]))
                }
                db.execute(
                    "UPDATE ai_catalog.search_generations SET manifest=%s",
                    (Jsonb(bad),),
                )
            try:
                assert http.get(profiler + "/readyz").status_code == 503
                reply = http.post(
                    profiler + "/api/internal/v1/ai/profile/extract-and-pool",
                    headers=auth,
                    json=body | {"sourceVersion": 8},
                )
                assert reply.status_code == 503
                assert http.get(budget + "/fixture/ledger").json() == ledger
            finally:
                with psycopg.connect(dburl) as db:
                    db.execute(
                        "UPDATE ai_catalog.search_generations SET manifest=%s",
                        (Jsonb(original),),
                    )
            assert http.get(profiler + "/readyz").status_code == 200
            result["checks"].append(
                "generation/order mismatch makes readyz+admission503 with zero additional budget/provider attempts"
            )
            with psycopg.connect(dburl) as db:
                mapping = db.execute(
                    "SELECT category_id,source_category_id FROM ai_catalog.integration_category_sources ORDER BY category_id LIMIT 1"
                ).fetchone()
                db.execute(
                    "UPDATE ai_catalog.integration_category_sources SET source_category_id=%s WHERE category_id=%s",
                    (mapping[1] + "-SYNTHETIC-CHANGED", mapping[0]),
                )
            try:
                assert http.get(profiler + "/readyz").status_code == 503
                assert http.get(budget + "/fixture/ledger").json() == ledger
            finally:
                with psycopg.connect(dburl) as db:
                    db.execute(
                        "UPDATE ai_catalog.integration_category_sources SET source_category_id=%s WHERE category_id=%s",
                        (mapping[1], mapping[0]),
                    )
            with psycopg.connect(dburl) as db:
                db.execute("UPDATE alembic_version SET version_num='0009'")
            try:
                assert http.get(profiler + "/readyz").status_code == 503
            finally:
                with psycopg.connect(dburl) as db:
                    db.execute("UPDATE alembic_version SET version_num='0008'")
            assert http.get(profiler + "/readyz").status_code == 200
            result["checks"].append(
                "source category mapping changes invalidate canonical manifest; unknown future migration is refused"
            )
            chat = start(
                "fixture-chat",
                "app.main:app",
                ROOT / "chat/.venv/bin/python",
                {
                    "PYTHONPATH": str(ROOT / "chat"),
                    "CHAT_RUNTIME_MODE": "mock",
                    "STORE_MODE": "postgres",
                    "DATABASE_URL": dburl.replace("fixture_owner@", "ai_chat_service@")
                    + "?options=-csearch_path%3Dai_chat,public",
                    "AI_SERVICE_TOKEN": "synthetic-token",
                    "SESSION_SECRET": "synthetic-session",
                    "QA_ADMIN_PASSWORD": "synthetic-admin",
                    "DEMO_ACCESS_TOKEN": "synthetic-token",
                    "SEARCH_URL": search,
                    "SEARCH_TOKEN": "synthetic-token",
                    "COOKIE_SECURE": "false",
                },
            )
            for _ in range(100):
                try:
                    if http.get(chat + "/readyz", headers=auth).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Free fixture Chat readiness failed")
            # Actual Console reads new llm shape; browser sends remain intercepted synthetic fixtures.

            config = BASE / "synthetic-console-targets.json"
            config.write_text(
                json.dumps(
                    {
                        "localFixtureProfilerFree": True,
                        "launcherPid": os.getpid(),
                        "label": "SYNTHETIC Emmet4930 QA",
                        "targets": {
                            "profiling": profiler,
                            "search": search,
                            "chat": chat,
                        },
                        "tokens": {
                            "profiling": "synthetic-token",
                            "chat": "synthetic-token",
                        },
                        "database": dburl,
                    }
                )
            )
            config.chmod(0o600)
            console = start(
                "fixture-console",
                "console.server:app",
                ROOT / "console/.venv/bin/python",
                {"CONSOLE_TARGET_FILE": str(config)},
            )
            wait(console + "/api/config")
            snapshot = http.get(console + "/api/snapshot").json()
            assert snapshot["services"]["profiling"]["status"] == "ready"
            assert (
                snapshot["services"]["profiling"]["data"]["llm"]["model"]
                == "openai/gpt-6-luna"
            )
            assert snapshot["collectionStatus"] == "collected"
            assert all(v["status"] == "ready" for v in snapshot["services"].values())
            result["checks"].append(
                "actual Console collector consumes new llm model/state metadata without inventing legacy tagger fields"
            )
            browser = subprocess.run(
                [
                    str(ROOT / "integration/.venv/bin/python"),
                    "-B",
                    str(ROOT / "integration/tests/check_emmet_console.py"),
                    console,
                    str(BASE / "browser"),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert browser.returncode == 0, browser.stderr[-2000:]
            result["checks"].append(
                "real Chrome renders new llm metadata and read-only unknown-budget scope"
            )
            faults = subprocess.run(
                [
                    str(ROOT / "integration/.venv/bin/python"),
                    "-B",
                    str(ROOT / "console/tests/browser_fault_probe.py"),
                    "--url",
                    console,
                    "--output",
                    str(BASE / "browser-faults"),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert faults.returncode == 0, faults.stderr[-2000:]
            fault_result = json.loads(
                (BASE / "browser-faults/browser-result.json").read_text()
            )
            assert fault_result["passed"]
            result["browserFaultChecks"] = len(fault_result["checks"])
            reply = http.post(profiler + "/fixture/stop-encoder")
            assert reply.json()["encoderStopped"]
            assert http.get(profiler + "/readyz").status_code == 503
            assert (
                http.post(
                    profiler + "/api/internal/v1/ai/profile/extract-and-pool",
                    headers=auth,
                    json=body | {"sourceVersion": 9},
                ).status_code
                == 503
            )
            assert http.get(budget + "/fixture/ledger").json() == ledger
            result["checks"].append(
                "required Node loss blocks readiness/admission before any further provider reservation"
            )
            result["consoleURL"] = console
            result["profilerURL"] = profiler
            result["allProviderCallsSynthetic"] = True
        owner.dispose()
        result["passed"] = True
        (BASE / "runtime-result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=30)
        for log in logs:
            log.close()
        if owner is not None:
            owner.dispose()
        if started:
            run([PG / "pg_ctl", "-D", BASE / "pg", "-m", "smart", "-w", "stop"])


if __name__ == "__main__":
    main()
