"""No-process/no-network checks for live startup and preserved resume state."""

import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from integration import launch_live_validation as launcher
from integration.live_preflight import (
    CREDENTIAL_NAMES,
    validate_credentials,
    validate_profiler_auth,
    validate_resume,
)


@pytest.mark.parametrize("token", [None, "", "   ", "\t"])
def test_required_profiler_needs_explicit_nonblank_token(token):
    for provider, required in [("openrouter", "false"), ("none", "true")]:
        with pytest.raises(ValueError):
            validate_profiler_auth(
                {
                    "PROFILING_TAGGER": provider,
                    "PROFILING_TAGGER_REQUIRED": required,
                    "PROFILING_SERVICE_TOKEN": token,
                }
            )


def test_nonblank_live_credentials_and_free_development_are_distinct():
    validate_credentials({key: "synthetic-value" for key in CREDENTIAL_NAMES})
    with pytest.raises(ValueError):
        validate_credentials(
            {
                key: "   " if key == "AI_SERVICE_TOKEN" else "synthetic"
                for key in CREDENTIAL_NAMES
            }
        )
    validate_profiler_auth(
        {
            "PROFILING_TAGGER": "openrouter",
            "PROFILING_TAGGER_REQUIRED": "true",
            "PROFILING_SERVICE_TOKEN": "synthetic",
        }
    )
    validate_profiler_auth(
        {"PROFILING_TAGGER": "none", "PROFILING_TAGGER_REQUIRED": "false"}
    )


@pytest.fixture
def saved(tmp_path):
    run, qa = tmp_path / "run", tmp_path / "ktb-ai-integration-persistent-test"
    run.mkdir()
    folder = qa / "search-root/data/generations/gen"
    folder.mkdir(parents=True)
    identity, version = str(uuid4()), str(uuid4())
    (qa / "search-root/data/CURRENT").write_text("gen\n")
    (folder / "catalog.json").write_text(
        json.dumps({"ai_catalog_version": version, "products": [{}] * 4231})
    )
    (qa / "provenance.json").write_text(json.dumps({"runIdentity": identity}))
    fence = tmp_path / "preserved.sqlite3"
    with sqlite3.connect(fence) as db:
        db.execute(
            "CREATE TABLE attempts(id TEXT PRIMARY KEY,amount REAL,model TEXT,ack INTEGER)"
        )
        for i in range(4):
            db.execute(
                "INSERT INTO attempts VALUES(?,?,?,1)",
                (str(i), 0.0125, "openai/gpt-6-luna"),
            )
    runtime = {
        "mode": "LIVE",
        "existingBudgetAccount": "gift-chat-harness-2026-10-06",
        "ownedQA": str(qa),
        "maxNewAttempts": 100,
        "maxNewReservationUsd": 1,
        "budgetStateFile": str(fence),
        "recipientUserId": 42,
        "sourceVersion": 1,
        "catalogVersion": version,
        "catalogProducts": 4231,
        "searchSnapshotId": "synthetic",
    }
    (run / "runtime.json").write_text(json.dumps(runtime))
    (run / "backend-state.json").write_text(
        json.dumps(
            {
                "synthetic": True,
                "recipientUserId": 42,
                "sourceVersion": 1,
                "callback": None,
                "giftPreference": "synthetic",
                "dislikedCategories": [],
            }
        )
    )
    return SimpleNamespace(
        run=run, qa=qa, fence=fence, identity=identity, runtime=runtime
    )


@pytest.mark.parametrize("missing", ["runtime.json", "backend-state.json", "fence"])
def test_resume_missing_state_never_recreates_it(saved, missing):
    path = saved.fence if missing == "fence" else saved.run / missing
    path.unlink()
    with pytest.raises(ValueError):
        validate_resume(saved.run, saved.qa, None, 100, 1)
    assert not path.exists()


def test_resume_omitted_path_uses_original_fence_unchanged(saved):
    before = saved.fence.read_bytes()
    callback = (saved.run / "backend-state.json").read_bytes()
    path, identity = validate_resume(saved.run, saved.qa, None, 100, 1)
    assert path == saved.fence and identity == saved.identity
    assert (
        saved.fence.read_bytes() == before
        and (saved.run / "backend-state.json").read_bytes() == callback
    )
    assert not (saved.run / "validation-attempts.sqlite3").exists()


@pytest.mark.parametrize(
    "fault",
    [
        "changed_fence",
        "wrong_qa",
        "guard_change",
        "corrupt_runtime",
        "wrong_catalog",
        "wrong_recipient",
        "corrupt_fence",
        "bad_callback",
    ],
)
def test_resume_rejects_changed_or_corrupt_binding(saved, fault):
    supplied = None
    qa = saved.qa
    limit = 100
    if fault == "changed_fence":
        supplied = saved.fence.parent / "another.sqlite3"
    elif fault == "wrong_qa":
        qa = qa.parent / "other"
    elif fault == "guard_change":
        limit = 99
    elif fault == "corrupt_runtime":
        (saved.run / "runtime.json").write_text("[")
    elif fault == "corrupt_fence":
        saved.fence.write_bytes(b"not sqlite")
    elif fault == "wrong_catalog":
        saved.runtime["catalogVersion"] = str(uuid4())
        (saved.run / "runtime.json").write_text(json.dumps(saved.runtime))
    else:
        state = json.loads((saved.run / "backend-state.json").read_text())
        if fault == "wrong_recipient":
            state["recipientUserId"] = 43
        else:
            state.pop("callback")
        (saved.run / "backend-state.json").write_text(json.dumps(state))
    with pytest.raises(ValueError):
        validate_resume(saved.run, qa, supplied, limit, 1)


def test_missing_resume_rejects_before_any_subprocess(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_VALIDATION_APPROVED", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic")
    monkeypatch.setattr(
        "sys.argv",
        [
            "launcher",
            "--resume",
            "--run-dir",
            str(tmp_path / "missing"),
            "--owned-qa-root",
            str(tmp_path / "qa"),
            "--pg-bin",
            str(tmp_path / "pg"),
            "--media-root",
            str(tmp_path / "media"),
        ],
    )
    real_open = Path.open

    def fake_open(path, *a, **kw):
        if str(path).endswith("/ai-chat-demo/.env"):
            return io.StringIO("".join(f"{k}=synthetic\n" for k in CREDENTIAL_NAMES))
        return real_open(path, *a, **kw)

    monkeypatch.setattr(Path, "open", fake_open)

    def forbidden(*a, **kw):
        raise AssertionError("subprocess must not run before resume validation")

    monkeypatch.setattr(launcher.subprocess, "run", forbidden)
    with pytest.raises(ValueError):
        launcher.main()
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_live_service_token_fails_before_any_process(
    tmp_path, monkeypatch, value
):
    monkeypatch.setenv("LIVE_VALIDATION_APPROVED", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic")
    monkeypatch.setattr(
        "sys.argv",
        [
            "launcher",
            "--run-dir",
            str(tmp_path / "run"),
            "--owned-qa-root",
            str(tmp_path / "qa"),
            "--pg-bin",
            str(tmp_path / "pg"),
            "--media-root",
            str(tmp_path / "media"),
        ],
    )
    real_open = Path.open

    def fake_open(path, *a, **kw):
        if str(path).endswith("/ai-chat-demo/.env"):
            return io.StringIO(
                "".join(
                    f"{k}={value if k == 'AI_SERVICE_TOKEN' else 'synthetic'}\n"
                    for k in CREDENTIAL_NAMES
                )
            )
        return real_open(path, *a, **kw)

    monkeypatch.setattr(Path, "open", fake_open)

    def forbidden(*a, **kw):
        raise AssertionError("No process allowed with missing live authentication")

    monkeypatch.setattr(launcher.subprocess, "run", forbidden)
    with pytest.raises(ValueError):
        launcher.main()
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize('valid', [True, False])
def test_resume_advanced_version_requires_committed_matching_binding(saved, valid):
    state = json.loads((saved.run / 'backend-state.json').read_text())
    state.update(sourceVersion=2, snapshotId='synthetic', callback={'recipientUserId': 42, 'sourceVersion': 2, 'profileStatus': 'COMPLETED', 'recommendedProductIds': []})
    state['profileInputs'] = {'2': {'fingerprint': 'a' * 64, 'giftPreference': state['giftPreference'] if valid else 'wrong', 'dislikedCategories': []}}
    (saved.run / 'backend-state.json').write_text(json.dumps(state))
    if valid:
        assert validate_resume(saved.run, saved.qa, None, 100, 1)[0] == saved.fence
    else:
        with pytest.raises(ValueError): validate_resume(saved.run, saved.qa, None, 100, 1)
