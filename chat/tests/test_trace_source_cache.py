"""Source fingerprints follow loaded code; request settings remain request-local."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.integrated_harness import protocol_manifest
from app.low_latency import LowLatencyRuntime
from app.mock_runtime import MockRuntime
from app.model_config import ACTIVE_MODEL, ModelConfig
from app.release import revision
from app.runtime import Runtime


APP = Path(__file__).resolve().parents[1] / "app"


def runtime(kind=Runtime, config=None):
    result = object.__new__(kind)
    result.model = "synthetic/default"
    result.catalog = SimpleNamespace(cached=lambda model: config)
    return result


def test_source_fingerprints_keep_existing_byte_order_and_digest_formula():
    names = ["runtime.py", "agent.py", "turn_report.py",
             "model_config.py", "profile.py", "public_answer.py",
             "conversation_memory.py", "streaming.py", "demo_profiles.py"]
    base = hashlib.sha256(b"".join((APP / name).read_bytes() for name in names)).hexdigest()[:16]
    low_paths = [APP / "low_latency.py", APP / "gift_adapter.py", APP / "model_trace.py",
                 *sorted((APP / "gift_core").glob("*.py"))]
    low = hashlib.sha256(base.encode() + b"".join(p.read_bytes() for p in low_paths)).hexdigest()[:16]
    assert runtime().trace_config()["promptVersion"] == base
    assert runtime(LowLatencyRuntime).trace_config()["promptVersion"] == low
    assert runtime(MockRuntime).trace_config()["promptVersion"] == hashlib.sha256(
        (APP / "mock_runtime.py").read_bytes()).hexdigest()[:16]

    record = protocol_manifest()
    for name, digest in record["sourceHashes"].items():
        assert digest == hashlib.sha256((APP / name).read_bytes()).hexdigest()
    assert record["sourceSha256"] == hashlib.sha256("".join(
        name + record["sourceHashes"][name] for name in sorted(record["sourceHashes"])
    ).encode()).hexdigest()
    assert record["integrationSourceSha256"] == hashlib.sha256(json.dumps(
        record["integrationSourceIdentity"], sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    assert revision() == json.loads((APP / "release.json").read_text())["revision"]


def test_repeated_traces_manifests_and_revision_do_not_read_or_hash_source(monkeypatch):
    instances = [runtime(kind) for kind in (Runtime, LowLatencyRuntime, MockRuntime)]
    expected = [item.trace_config() for item in instances]
    expected_protocol, expected_revision = protocol_manifest(), revision()
    sha256 = hashlib.sha256
    inputs = []

    def deny(*args, **kwargs):
        raise AssertionError("Request-time source filesystem access")

    def observe(data=b"", *args, **kwargs):
        inputs.append(data)
        return sha256(data, *args, **kwargs)

    with monkeypatch.context() as patch:
        for name in ("read_bytes", "read_text", "open", "glob", "is_file", "exists"):
            patch.setattr(Path, name, deny)
        patch.setattr(hashlib, "sha256", observe)
        for _ in range(25):
            assert [item.trace_config() for item in instances] == expected
            assert protocol_manifest() == expected_protocol
            assert revision() == expected_revision
    # Settings still have their own dynamic digest; no source bytes are rehashed.
    assert inputs and all(json.loads(value)["model"] == "synthetic/default" for value in inputs)


@pytest.mark.parametrize("kind", [Runtime, LowLatencyRuntime])
def test_model_settings_and_environment_remain_dynamic_and_detached(kind, monkeypatch):
    first = ModelConfig("synthetic/first", frozenset(),
                        {"enabled": False, "nested": {"values": ["original"]}}, False, 1200)
    second = ModelConfig("synthetic/second", frozenset(), {"enabled": True}, True, 4096)
    instance = runtime(kind, first)
    monkeypatch.setenv("CHAT_AGENT_MODE", "first-mode")
    before = instance.trace_config()
    pristine = copy.deepcopy(before)
    before["settings"]["reasoning"]["nested"]["values"].append("polluted")
    before["settings"]["model"] = "polluted"
    assert instance.trace_config() == pristine
    assert first.reasoning["nested"]["values"] == ["original"]

    monkeypatch.setenv("CHAT_AGENT_MODE", "second-mode")
    token = ACTIVE_MODEL.set(second)
    try:
        after = instance.trace_config()
    finally:
        ACTIVE_MODEL.reset(token)
    assert after["promptVersion"] == pristine["promptVersion"]
    assert after["settingsVersion"] != pristine["settingsVersion"]
    assert after["settings"]["model"] == second.model
    assert after["settings"]["reasoning"] == second.reasoning
    assert after["settings"]["agent"] == ("second-mode" if kind is Runtime else instance.agent_name)
    assert after["settingsVersion"] == hashlib.sha256(json.dumps(
        after["settings"], sort_keys=True).encode()).hexdigest()[:16]


def test_manifest_mutation_does_not_contaminate_next_receipt():
    before = protocol_manifest()
    polluted = protocol_manifest()
    polluted["sourceHashes"]["low_latency.py"] = "polluted"
    polluted["retrievalSourceHashes"].clear()
    polluted["integrationSourceIdentity"]["other"].clear()
    polluted["integrationSourceIdentity"]["unavailable"].append("polluted")
    polluted["version"] = "polluted"
    assert protocol_manifest() == before


def test_import_pins_first_call_and_restart_refreshes_modified_sources(tmp_path):
    checkout = tmp_path / "checkout"
    app = checkout / "chat/app"
    shutil.copytree(APP, app, ignore=shutil.ignore_patterns("__pycache__"))
    # Only source files referenced by the existing cross-service manifest.
    paths = {"product-search/search/" + name for name in protocol_manifest()["retrievalSourceHashes"]}
    paths.update(protocol_manifest()["integrationSourceIdentity"]["other"])
    source_checkout = APP.parents[1]
    for name in paths:
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_checkout / name, target)
    code = r'''
import hashlib,json,sys
from pathlib import Path
from app.runtime import Runtime
from app.low_latency import LowLatencyRuntime
from app.mock_runtime import MockRuntime
from app.integrated_harness import protocol_manifest
from app.release import revision
root=Path(sys.argv[1])
names=['runtime.py','agent.py','turn_report.py','model_config.py','profile.py','public_answer.py','conversation_memory.py','streaming.py','demo_profiles.py']
base=hashlib.sha256(b''.join((root/name).read_bytes() for name in names)).hexdigest()[:16]
low=hashlib.sha256(base.encode()+b''.join(p.read_bytes() for p in [root/'low_latency.py',root/'gift_adapter.py',root/'model_trace.py',*sorted((root/'gift_core').glob('*.py'))])).hexdigest()[:16]
mock=hashlib.sha256((root/'mock_runtime.py').read_bytes()).hexdigest()[:16]
integrated=hashlib.sha256((root/'integrated_harness.py').read_bytes()).hexdigest()
release=json.loads((root/'release.json').read_text())['revision']
if len(sys.argv)>2:
    # Edit after imports but before any trace/revision/manifest getter was called.
    for name in ['public_answer.py','integrated_harness.py','mock_runtime.py']:
        with (root/name).open('a') as stream:stream.write('\n# new process source\n')
    (root/'release.json').write_text(json.dumps({'revision':'new-process-revision'}))
values=[]
for kind in [Runtime,LowLatencyRuntime,MockRuntime]:
    instance=object.__new__(kind);instance.model='synthetic/default'
    values.append(instance.trace_config()['promptVersion'])
record=protocol_manifest()
assert values == [base,low,mock]
assert record['sourceHashes']['integrated_harness.py']==integrated
assert revision()==release
print(json.dumps({'versions':values,'protocol':record['sourceSha256'],'integration':record['integrationSourceSha256'],'release':revision()}))
'''
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    env.update(PYTHONPATH=str(app.parent), PYTHONDONTWRITEBYTECODE="1")

    def inspect(*extra):
        result = subprocess.run([sys.executable, "-B", "-c", code, str(app), *extra],
                                cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    loaded = inspect("edit-after-import")
    restarted = inspect()
    assert all(before != after for before, after in zip(loaded["versions"], restarted["versions"]))
    assert loaded["protocol"] != restarted["protocol"]
    assert loaded["integration"] != restarted["integration"]
    assert loaded["release"] != restarted["release"] == "new-process-revision"
