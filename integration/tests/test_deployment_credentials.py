"""Deployment injection and dashboard precedence; synthetic values, no AWS/DB."""
from concurrent.futures import ThreadPoolExecutor
import json
import os

import pytest

from integration.provider_key import KeyConflict, KeyUnavailable, ProviderKey
from integration.service_token import ServiceToken


def key(label):
    return "sk-or-v1-" + ("synthetic_" + label + "_") * 4


def sources(env):
    return [ProviderKey.from_environment("chat", env), ProviderKey.from_environment("profiler", env),
            ServiceToken.from_environment("chat", env)]


def test_single_team_secret_fields_work_and_dashboard_overrides_survive_restart(tmp_path):
    # The deployment bridge injects these fields; application code never edits
    # the source JSON or needs permission to write to Secrets Manager.
    team = {"OPENROUTER_API_KEY": key("chat"), "PROFILING_OPENROUTER_API_KEY": key("profiler"),
            "BE_SERVICE_TOKEN": "synthetic_common_BE_" * 3, "OTHER_TEAM_FIELD": "preserve"}
    before = json.dumps(team, sort_keys=True)
    env = {**team, "AI_ENVIRONMENT": "develop", "OPENROUTER_MANAGED_KEY_DIR": str(tmp_path),
           "BE_SERVICE_TOKEN_DIR": str(tmp_path)}
    initial = sources(env)
    assert [s.snapshot().value for s in initial] == list(team.values())[:3]
    assert all(s.status()["source"] == "deployment" and s.status()["writable"] for s in initial)
    replacements = [key("saved_chat"), key("saved_profiler"), "synthetic_saved_BE_" * 3]
    revisions = [s.save(v, None)["revision"] for s, v in zip(initial, replacements)]
    env.update(OPENROUTER_API_KEY=key("later_deploy_chat"),
               PROFILING_OPENROUTER_API_KEY=key("later_deploy_profiler"), BE_SERVICE_TOKEN="synthetic_later_BE_" * 3)
    restarted = sources(env)
    assert [s.snapshot().value for s in restarted] == replacements
    assert [s.snapshot().revision for s in restarted] == revisions
    assert all(s.status()["source"] == "dashboard" for s in restarted)
    assert ServiceToken.from_environment("profiler", env).snapshot().value == replacements[2]
    assert json.dumps(team, sort_keys=True) == before
    assert all(value not in json.dumps([s.status() for s in restarted]) for value in replacements)


@pytest.mark.parametrize("kind", ["chat", "profiler", "backend"])
@pytest.mark.parametrize("fault", ["missing", "corrupt", "wrong_environment"])
def test_saved_record_failure_never_restores_deployment_value(tmp_path, kind, fault):
    cls = ServiceToken if kind == "backend" else ProviderKey
    service = "chat" if kind == "backend" else kind
    value = "synthetic_BE_token_" * 3 if kind == "backend" else key("first")
    source = cls(kind=service, managed_directory=tmp_path, value=value, env="develop")
    source.save(value, None)
    path = tmp_path / source.record_file
    if fault == "missing": path.unlink()
    elif fault == "corrupt": path.write_text("corrupt")
    else:
        record = json.loads(path.read_text()); record["environment"] = "production"
        path.write_text(json.dumps(record))
    restarted = cls(kind=service, managed_directory=tmp_path, value=value, env="develop")
    with pytest.raises(KeyUnavailable): restarted.snapshot()
    assert not restarted.status()["configured"]


def test_file_precedes_env_only_before_first_dashboard_save(tmp_path):
    external = tmp_path / "injected"; external.write_text(key("file"))
    env = {"OPENROUTER_API_KEY": key("env"), "OPENROUTER_API_KEY_FILE": str(external),
           "OPENROUTER_MANAGED_KEY_DIR": str(tmp_path)}
    source = ProviderKey.from_environment(env)
    assert source.snapshot().value == key("file")
    external.unlink()
    with pytest.raises(KeyUnavailable): source.snapshot()
    # An invalid/missing injection can be repaired by a deliberate dashboard save.
    source.save(key("dashboard"), None)
    assert ProviderKey.from_environment(env).snapshot().value == key("dashboard")


def test_invalid_deployment_file_does_not_block_deliberate_dashboard_repair(tmp_path):
    path = tmp_path / "injected"; path.write_text("invalid token with spaces")
    source = ProviderKey(managed_directory=tmp_path, external_file=str(path))
    assert source.status()["state"] == "unavailable" and source.status()["writable"]
    source.save(key("repaired"), None)
    assert source.snapshot().value == key("repaired")
    assert path.read_text() == "invalid token with spaces"


def test_first_dashboard_writers_cannot_overwrite_each_other(tmp_path):
    source = ProviderKey(managed_directory=tmp_path, value=key("injected"))
    def write(label):
        try: return source.save(key(label), None)
        except KeyConflict: return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["one", "two"]))
    assert results.count("conflict") == 1
    assert source.status()["revision"] == next(r["revision"] for r in results if isinstance(r, dict))


@pytest.mark.parametrize("kind", ["chat", "profiler", "backend"])
@pytest.mark.parametrize("fault", ["replace", "file_fsync", "marker_fsync", "read_only"])
def test_failed_first_save_preserves_initial_value_and_retry(tmp_path, monkeypatch, kind, fault):
    cls = ServiceToken if kind == "backend" else ProviderKey
    service = "chat" if kind == "backend" else kind
    initial = "synthetic_initial_BE_" * 3 if kind == "backend" else key("injected")
    replacement = "synthetic_saved_BE_" * 3 if kind == "backend" else key("dashboard")
    source = cls(kind=service, managed_directory=tmp_path, value=initial)
    with monkeypatch.context() as patch:
        if fault == "replace":
            patch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("synthetic failure")))
        elif fault in {"file_fsync", "marker_fsync"}:
            original = os.fsync; calls = []
            def sync(fd):
                calls.append(fd)
                if len(calls) == (1 if fault == "file_fsync" else 2):
                    raise OSError("synthetic fsync failure")
                return original(fd)
            patch.setattr(os, "fsync", sync)
        else:
            original = os.open
            def readonly(path, flags, *args):
                if flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR):
                    raise PermissionError("synthetic read-only mount")
                return original(path, flags, *args)
            patch.setattr(os, "open", readonly)
        with pytest.raises(KeyUnavailable, match="SAVE_FAILED"):
            source.save(replacement, None)
    restarted = cls(kind=service, managed_directory=tmp_path, value=initial)
    assert restarted.snapshot().value == initial and restarted.snapshot().revision is None
    assert not restarted.saved_marker.exists() and not list(tmp_path.glob(".key-*.tmp"))
    restarted.save(replacement, None)
    assert restarted.snapshot().value == replacement and restarted.saved_marker.exists()


def test_failed_first_save_marker_cleanup_failure_is_uncertain(tmp_path, monkeypatch):
    from pathlib import Path
    source = ProviderKey(managed_directory=tmp_path, value=key("injected"))
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("synthetic failure")))
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path == source.saved_marker:
            raise OSError("synthetic cleanup failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(KeyUnavailable, match="SAVE_UNCONFIRMED"):
        source.save(key("dashboard"), None)
    with pytest.raises(KeyUnavailable):
        ProviderKey(managed_directory=tmp_path, value=key("injected")).snapshot()


def test_interrupted_first_save_keeps_no_fallback_boundary(tmp_path, monkeypatch):
    class InterruptedSave(BaseException): pass
    source = ProviderKey(managed_directory=tmp_path, value=key("injected"))
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(InterruptedSave()))
    with pytest.raises(InterruptedSave): source.save(key("dashboard"), None)
    assert source.saved_marker.exists()
    with pytest.raises(KeyUnavailable):
        ProviderKey(managed_directory=tmp_path, value=key("injected")).snapshot()
