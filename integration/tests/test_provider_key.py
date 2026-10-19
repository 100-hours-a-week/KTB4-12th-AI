from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets

import pytest

from integration.provider_key import ProviderKey, KeyUnavailable, KeyConflict, KEY_FILE


def fake_key():
    return "sk-or-v1-" + secrets.token_hex(32)


def test_atomic_write_restart_and_write_only_status(tmp_path):
    source = ProviderKey(managed_directory=str(tmp_path)); key = fake_key()
    assert source.status()["state"] == "missing"
    saved = source.save(key, None)
    assert key not in json.dumps(saved) and key not in repr(source.snapshot())
    assert source.snapshot().value == key and source.status() == saved
    assert (tmp_path/KEY_FILE).stat().st_mode & 0o777 == 0o600
    restarted = ProviderKey(managed_directory=str(tmp_path))
    assert restarted.snapshot().value == key
    new = fake_key(); previous = source.snapshot()
    later = restarted.save(new, saved["revision"])
    assert later["revision"] != saved["revision"] and source.snapshot().value == new
    assert previous.value == key  # In-flight immutable snapshot is retained.
    assert not list(tmp_path.glob(".key-*.tmp"))


def test_competing_writers_compare_revision(tmp_path):
    source = ProviderKey(managed_directory=str(tmp_path))
    saved = source.save(fake_key(), None)
    def write():
        try:return source.save(fake_key(), saved["revision"])
        except KeyConflict:return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:write(), range(2)))
    assert results.count("conflict") == 1
    assert source.status() == next(x for x in results if isinstance(x, dict))


@pytest.mark.parametrize("failure", ["replace", "file_fsync", "read_only"])
def test_failed_write_keeps_previous_key_and_no_partial_secret(tmp_path, monkeypatch, failure):
    source = ProviderKey(managed_directory=str(tmp_path)); old = fake_key()
    saved = source.save(old, None)
    if failure == "replace":
        monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("SYNTHETIC_PRIVATE_DETAIL")))
    elif failure == "file_fsync":
        monkeypatch.setattr(os, "fsync", lambda *a: (_ for _ in ()).throw(OSError("SYNTHETIC_PRIVATE_DETAIL")))
    else:
        original = os.open
        def readonly(path, flags, *args):
            if flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR): raise PermissionError("read-only synthetic mount")
            return original(path, flags, *args)
        monkeypatch.setattr(os, "open", readonly)
    with pytest.raises(KeyUnavailable) as result:source.save(fake_key(), saved["revision"])
    assert "SYNTHETIC" not in str(result.value) and source.snapshot().value == old
    assert not list(tmp_path.glob(".key-*.tmp"))


def test_directory_fsync_failure_is_reported_as_uncertain(tmp_path, monkeypatch):
    source = ProviderKey(managed_directory=str(tmp_path)); saved = source.save(fake_key(), None)
    new = fake_key(); original = os.fsync; calls=[]
    def sync(fd):
        calls.append(True)
        if len(calls) == 2:raise OSError("synthetic directory sync failed")
        return original(fd)
    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(KeyUnavailable, match="SAVE_UNCONFIRMED"):source.save(new, saved["revision"])
    assert source.snapshot().value == new  # Do not claim rollback after rename.


def test_external_file_never_written_and_conflicting_sources_rejected(tmp_path):
    path = tmp_path/"external"; key = fake_key(); path.write_text(key)
    external = ProviderKey(external_file=str(path))
    with pytest.raises(KeyConflict):external.save(fake_key(), None)
    assert path.read_text() == key and external.snapshot().value == key
    for other in ({"OPENROUTER_API_KEY_FILE": str(path)}, {"OPENROUTER_API_KEY": key}):
        source = ProviderKey.from_environment({"OPENROUTER_MANAGED_KEY_DIR": str(tmp_path), **other})
        assert source.snapshot().value == key and source.status()["source"] == "deployment"
    with pytest.raises(KeyUnavailable, match="SOURCE_CONFLICT"):
        ProviderKey.from_environment({"OPENROUTER_MANAGED_KEY_DIR": str(tmp_path), "OPENROUTER_EXTERNAL_KEY_CONFIGURED": "1"})


def test_unsafe_missing_corrupt_or_public_store_fails_closed(tmp_path):
    source = ProviderKey(managed_directory=str(tmp_path)); path = tmp_path/KEY_FILE
    with pytest.raises(KeyUnavailable):source.snapshot()
    path.write_text('{"key":"SYNTHETIC_CANARY"}'); path.chmod(0o600)
    with pytest.raises(KeyUnavailable):source.snapshot()
    with pytest.raises(KeyUnavailable):source.save(fake_key(), None)
    assert "CANARY" not in json.dumps(source.status())
    path.unlink(); outside=tmp_path/"outside"; outside.write_text(fake_key()); path.symlink_to(outside)
    with pytest.raises(KeyUnavailable):source.snapshot()
    path.unlink(); tmp_path.chmod(0o755)
    with pytest.raises(KeyUnavailable):ProviderKey(managed_directory=str(tmp_path))


@pytest.mark.parametrize("value", ["", "Bearer SYNTHETIC_CANARY", "sk-or-v1-"+"a"*32+"\n", {"key":"SYNTHETIC_CANARY"}])
def test_invalid_key_never_persisted_or_echoed(tmp_path, value):
    source = ProviderKey(managed_directory=str(tmp_path))
    with pytest.raises(KeyUnavailable) as failure:source.save(value, None)
    assert "CANARY" not in str(failure.value) and not (tmp_path/KEY_FILE).exists()


@pytest.mark.parametrize("revision", [1, {}, [], None, "not-a-uuid"])
def test_corrupt_revision_is_unavailable_and_not_replaced(tmp_path, revision):
    source = ProviderKey(managed_directory=str(tmp_path))
    source.save(fake_key(), None)
    path = tmp_path/KEY_FILE; record = json.loads(path.read_text()); record["revision"] = revision
    path.write_text(json.dumps(record)); before = path.read_bytes()
    assert source.status()["state"] == "unavailable"
    with pytest.raises(KeyUnavailable): source.save(fake_key(), None)
    assert path.read_bytes() == before
