"""Read-only live status; never submits turns, jobs, callbacks or paid requests."""

try:
    from integration.profiler_health import model_state
except ModuleNotFoundError:
    from profiler_health import model_state

import argparse
import hashlib
import json
import math
import subprocess
from pathlib import Path

import httpx
import psycopg


def readiness_errors(service, body, runtime):
    """Semantic readiness is distinct from a successful HTTP transport."""
    if not isinstance(body, dict):
        return ["response is not an object"]
    errors = []

    def require(condition, message):
        if not condition:
            errors.append(message)

    if service == "console":
        require(
            body.get("mode") == "LOCAL_TARGET"
            and body.get("paidCallsEnabled") is True
            and body.get("profileSendAllowed") is True,
            "console is not the explicit live target",
        )
        build = body.get("build") or {}
        require(
            isinstance(build, dict)
            and build.get("serverSha256")
            == runtime["sourceHashes"].get("console/server.py"),
            "console process source differs",
        )
    elif service == "chat":
        require(
            body.get("ready") is True
            and body.get("database") == "postgresql"
            and body.get("mode") == "live"
            and body.get("model") == runtime["model"],
            "Chat live runtime is not ready",
        )
        require(
            body.get("budgetAccount") == runtime["existingBudgetAccount"],
            "Chat budget account differs",
        )
        budget = body.get("sharedBudget")
        if not isinstance(budget, dict):
            errors.append("shared budget missing")
        else:
            require(
                budget.get("limitUsd") == 5
                and "halted" in budget
                and budget.get("halted") is None,
                "shared budget unavailable/halted",
            )
            require(
                budget.get("validationMaxAttempts") == runtime["maxNewAttempts"]
                and budget.get("validationMaxReservedUsd")
                == runtime["maxNewReservationUsd"],
                "validation guard differs",
            )
            for key in (
                "committedUsd",
                "remainingUsd",
                "attempts",
                "validationAttempts",
                "validationReservedUsd",
            ):
                value = budget.get(key)
                require(
                    type(value) in (int, float) and math.isfinite(value) and value >= 0,
                    "budget field missing/invalid: " + key,
                )
            if not errors:
                require(
                    budget["remainingUsd"] > 0
                    and budget["validationAttempts"] < runtime["maxNewAttempts"]
                    and budget["validationReservedUsd"]
                    < runtime["maxNewReservationUsd"],
                    "live submission budget exhausted",
                )
    elif service == "search":
        require(
            body.get("status") == "ready" and body.get("catalogLoadError") is None,
            "Search catalog is not ready",
        )
        require(
            body.get("products") == runtime["catalogProducts"]
            and body.get("snapshotId") == runtime["searchSnapshotId"],
            "Search products/snapshot differs",
        )
    elif service == "profiling":
        catalog, store, tagger, embedding = (
            body.get(key) for key in ("catalog", "store", "tagger", "embedding")
        )
        tagger=model_state(body)
        require(body.get("status") == "ok", "Profiler status is not ok")
        require(
            isinstance(catalog, dict)
            and catalog.get("active") is True
            and catalog.get("version") == runtime["catalogVersion"]
            and catalog.get("products") == runtime["catalogProducts"],
            "Profiler catalog inactive/different",
        )
        require(
            isinstance(store, dict)
            and store.get("connected") is True
            and store.get("backend") == "db",
            "Profiler store disconnected/different",
        )
        require(
            isinstance(tagger, dict)
            and tagger.get("enabled") is True
            and tagger.get("required") is True
            and tagger.get("model") == runtime["model"],
            "Profiler required real tagger unavailable",
        )
        require(
            isinstance(embedding, dict) and embedding.get("enabled") is True,
            "Profiler embedding unavailable",
        )
    else:
        errors.append("unknown service")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    run = parser.parse_args().run_dir.resolve()
    runtime = json.loads((run / "runtime.json").read_text())
    private = json.loads((run / "targets.json").read_text())
    command = subprocess.run(
        ["ps", "-p", str(runtime["launcherPid"]), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    owned = (
        command.returncode == 0
        and "integration/launch_live_validation.py" in command.stdout
        and str(run) in command.stdout
    )
    report = {
        "launcherOwned": owned,
        "console": runtime["url"],
        "services": {},
        "readinessErrors": {},
    }
    if not owned:
        print(json.dumps(report))
        raise SystemExit(1)
    with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
        for service, path in (
            ("console", "/api/config"),
            ("chat", "/readyz"),
            ("search", "/readyz"),
            ("profiling", "/health"),
        ):
            token = private["tokens"].get(service)
            try:
                response = client.get(
                    runtime["urls"][service] + path,
                    headers={"Authorization": "Bearer " + token} if token else {},
                )
                report["services"][service] = response.status_code
                try:
                    report["readinessErrors"][service] = (
                        readiness_errors(service, response.json(), runtime)
                        if response.status_code == 200
                        else ["HTTP readiness failed"]
                    )
                except (ValueError, KeyError, TypeError):
                    report["readinessErrors"][service] = [
                        "malformed/missing readiness fields"
                    ]
            except httpx.HTTPError:
                report["services"][service] = "unreachable"
                report["readinessErrors"][service] = ["unreachable"]
    dburl = f"postgresql://fixture_owner@127.0.0.1:{runtime['databasePort']}/{runtime.get('databaseName','emmet4930_console')}"
    with (
        psycopg.connect(
            dburl, connect_timeout=5, options="-c default_transaction_read_only=on"
        ) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT count(*) FROM ai_chat.turns WHERE state->>'status'='PROCESSING'"
        )
        report["processingChatTurns"] = cursor.fetchone()[0]
        cursor.execute(
            "SELECT count(*) FROM ai_profile.profile_runs WHERE status='RUNNING'"
        )
        report["runningProfiles"] = cursor.fetchone()[0]
    root = Path(runtime["sourceRoot"])
    report["sourceDrift"] = [
        name
        for name, digest in runtime["sourceHashes"].items()
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest
    ]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if (
        any(value != 200 for value in report["services"].values())
        or any(report["readinessErrors"].values())
        or report["sourceDrift"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
