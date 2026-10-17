"""HTTP success must not conceal unready/mock live integration services."""

import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from integration import live_status as status


@pytest.fixture
def healthy(tmp_path):
    source = tmp_path / "console/server.py"
    source.parent.mkdir()
    source.write_text("synthetic-source")
    runtime = {
        "launcherPid": 7,
        "url": "http://127.0.0.1:18800",
        "urls": {
            k: "http://127.0.0.1:" + str(18000 + i)
            for i, k in enumerate(["console", "chat", "search", "profiling"])
        },
        "sourceRoot": str(tmp_path),
        "sourceHashes": {
            "console/server.py": hashlib.sha256(source.read_bytes()).hexdigest()
        },
        "databasePort": 12345,
        "model": "openai/gpt-6-luna",
        "existingBudgetAccount": "gift-chat-harness-2026-10-06",
        "catalogProducts": 4231,
        "catalogVersion": "synthetic-generation",
        "searchSnapshotId": "synthetic-snapshot",
        "maxNewAttempts": 100,
        "maxNewReservationUsd": 1,
    }
    body = {
        "console": {
            "mode": "LOCAL_TARGET",
            "paidCallsEnabled": True,
            "profileSendAllowed": True,
            "build": {"serverSha256": runtime["sourceHashes"]["console/server.py"]},
        },
        "chat": {
            "ready": True,
            "database": "postgresql",
            "mode": "live",
            "model": runtime["model"],
            "budgetAccount": runtime["existingBudgetAccount"],
            "sharedBudget": {
                "limitUsd": 5,
                "halted": None,
                "validationMaxAttempts": 100,
                "validationMaxReservedUsd": 1,
                "committedUsd": 3.6,
                "remainingUsd": 1.4,
                "attempts": 144,
                "validationAttempts": 4,
                "validationReservedUsd": 0.05,
            },
        },
        "search": {
            "status": "ready",
            "products": 4231,
            "snapshotId": runtime["searchSnapshotId"],
            "catalogLoadError": None,
        },
        "profiling": {
            "status": "ok",
            "catalog": {
                "active": True,
                "version": runtime["catalogVersion"],
                "products": 4231,
            },
            "store": {"connected": True, "backend": "db"},
            "tagger": {"enabled": True, "required": True, "model": runtime["model"]},
            "embedding": {"enabled": True},
        },
    }
    return runtime, body


@pytest.mark.parametrize(
    "service,path,value",
    [
        ("console", ("mode",), "DEMO"),
        ("console", ("paidCallsEnabled",), False),
        ("chat", ("ready",), False),
        ("chat", ("mode",), "mock"),
        ("chat", ("model",), "unexpected"),
        ("chat", ("sharedBudget",), {}),
        ("chat", ("sharedBudget", "halted"), "uncertain"),
        ("chat", ("sharedBudget", "remainingUsd"), 0),
        ("chat", ("sharedBudget", "validationAttempts"), 100),
        ("search", ("status",), "loading"),
        ("search", ("snapshotId",), "stale"),
        ("search", ("products",), 3),
        ("profiling", ("catalog", "active"), False),
        ("profiling", ("catalog", "version"), "stale"),
        ("profiling", ("store", "connected"), False),
        ("profiling", ("tagger", "required"), False),
        ("profiling", ("embedding", "enabled"), False),
    ],
)
def test_semantic_failure_rejected(healthy, service, path, value):
    runtime, bodies = healthy
    body = copy.deepcopy(bodies[service])
    target = body
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert status.readiness_errors(service, body, runtime)


@pytest.mark.parametrize("service", ["console", "chat", "search", "profiling"])
def test_consistent_ready_and_missing_schema_are_distinct(healthy, service):
    runtime, bodies = healthy
    assert status.readiness_errors(service, bodies[service], runtime) == []
    for malformed in [None, [], {}, "unknown"]:
        assert status.readiness_errors(service, malformed, runtime)


@pytest.mark.parametrize("fault", [None, "mock", "malformed"])
def test_command_exit_reflects_semantics_not_only_http200(
    tmp_path, monkeypatch, capsys, healthy, fault
):
    runtime, bodies = healthy
    run = tmp_path / "run"
    run.mkdir()
    (run / "runtime.json").write_text(json.dumps(runtime))
    (run / "targets.json").write_text(json.dumps({"tokens": {}}))
    monkeypatch.setattr("sys.argv", ["status", "--run-dir", str(run)])
    monkeypatch.setattr(
        status.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=0,
            stdout="python integration/launch_live_validation.py --run-dir " + str(run),
        ),
    )
    urls = {value: key for key, value in runtime["urls"].items()}
    real_client = httpx.Client

    def response(request):
        service = urls[f"{request.url.scheme}://{request.url.host}:{request.url.port}"]
        if service == "chat" and fault == "malformed":
            return httpx.Response(200, text="not json")
        if service == "chat" and fault == "mock":
            return httpx.Response(200, json={"mode": "mock", "ready": False})
        return httpx.Response(200, json=bodies[service])

    monkeypatch.setattr(
        status.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(response), **kw),
    )

    class DB:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def cursor(self):
            return self

        def execute(self, *a):
            pass

        def fetchone(self):
            return (0,)

    def connect(*a, **kw):
        assert kw["options"] == "-c default_transaction_read_only=on"
        return DB()

    monkeypatch.setattr(status.psycopg, "connect", connect)
    if fault:
        with pytest.raises(SystemExit) as error:
            status.main()
        assert error.value.code == 1
    else:
        status.main()
    result = json.loads(capsys.readouterr().out)
    assert all(code == 200 for code in result["services"].values())
    assert bool(result["readinessErrors"]["chat"]) == bool(fault)
