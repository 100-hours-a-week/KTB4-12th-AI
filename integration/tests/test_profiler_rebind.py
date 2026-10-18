"""Exercise real Emmet worker queues with synthetic catalog adapters and no providers."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from profiling.intake import settings_dep
from profiling.ports import NoActiveCatalog
from profiling.supervisor import Supervisor

from integration.profiler_rebind import (
    REFRESH_PATH,
    ManagedSearch,
    RefreshFailure,
    refresh_catalog,
    require_refresh_token,
)


class Catalog:
    def __init__(self, name):
        self.snapshot = self.catalog_version = self.version = name
        self.total = self.matched = 1
        self.closed = 0
        self.read_entered = self.read_release = None

    def check(self):
        assert not self.closed
        return self.catalog_version

    def active(self):
        self.check()
        if self.read_entered:
            self.read_entered.set()
            assert self.read_release.wait(2)
        return self.catalog_version, []

    def encode(self, texts):
        self.check()
        return np.eye(768)[: len(texts)]

    def close(self):
        self.closed += 1


@pytest.fixture
def managed():
    old, new = Catalog("old"), Catalog("new")
    supervisor = Supervisor(profiling_slots=1, queue_max=4)
    supervisor.start()
    manager = ManagedSearch(old, supervisor, lambda: new)
    yield manager, old, new
    manager.stop(2)
    manager.close()


def wait_paused(manager):
    with manager.condition:
        assert manager.condition.wait_for(lambda: manager.paused, 2)


def test_refresh_drains_running_and_queued_jobs_and_rejects_new_admission(managed):
    manager, old, new = managed
    entered, release = threading.Event(), threading.Event()
    completed = []

    def job(block=False):
        if block:
            entered.set()
            assert release.wait(2)
        completed.append(manager.active()[0])

    assert manager.submit(job, True) and entered.wait(2)
    assert manager.submit(job)
    with ThreadPoolExecutor(1) as pool:
        refresh = pool.submit(manager.refresh, "new", 2)
        wait_paused(manager)
        assert not manager.submit(job)
        with pytest.raises(NoActiveCatalog):
            manager.active()
        assert not old.closed and not completed
        release.set()
        assert refresh.result(2)["changed"]
    assert completed == ["old", "old"] and old.closed == 1
    assert manager.active()[0] == "new" and not manager.status()["admissionPaused"]
    assert manager.submit(job)
    with manager.condition:
        assert manager.condition.wait_for(lambda: not manager.pending, 2)
    assert completed[-1] == "new" and not new.closed


def test_drain_timeout_preserves_binding_and_resumes_admission(managed):
    manager, old, _ = managed
    entered, release = threading.Event(), threading.Event()
    assert manager.submit(lambda: (entered.set(), release.wait(2)))
    assert entered.wait(2)
    try:
        with pytest.raises(RefreshFailure, match="CATALOG_DRAIN_TIMEOUT"):
            manager.refresh("new", 0.01)
        assert manager.current is old and not old.closed and not manager.paused
    finally:
        release.set()


@pytest.mark.parametrize('unfinished', [1, 3])
def test_empty_queue_cannot_rebind_with_durable_unfinished_work(managed, unfinished):
    manager, old, _ = managed
    manager.unfinished_runs = lambda: unfinished
    assert manager.pending == 0
    with pytest.raises(RefreshFailure, match='CATALOG_DURABLE_WORK_REMAINS'):
        manager.refresh('new', 1)
    assert manager.current is old and not old.closed and not manager.paused
    manager.unfinished_runs = lambda: 0
    assert manager.refresh('new', 1)['changed']


def test_durable_run_check_failure_cannot_rebind(managed):
    manager, old, _ = managed
    manager.unfinished_runs = lambda: (_ for _ in ()).throw(RuntimeError('synthetic DB down'))
    with pytest.raises(RefreshFailure, match='CATALOG_REFRESH_FAILED'):
        manager.refresh('new', 1)
    assert manager.current is old and not old.closed


@pytest.mark.parametrize("failure", ["validation", "wrong-target"])
def test_failed_candidate_keeps_previous_generation_and_drops_candidate(
    managed, failure
):
    manager, old, new = managed
    if failure == "validation":

        def fail():
            raise NoActiveCatalog("synthetic private failure")

        manager.factory = fail
    with pytest.raises(RefreshFailure) as caught:
        manager.refresh("new" if failure == "validation" else "different", 1)
    assert str(caught.value) in ("CATALOG_REFRESH_FAILED", "TARGET_SNAPSHOT_MISMATCH")
    assert manager.active()[0] == "old" and not old.closed and not manager.paused
    assert new.closed == (failure == "wrong-target")


def test_refresh_same_snapshot_is_idempotent(managed):
    manager, old, _ = managed
    manager.factory = lambda: pytest.fail("Already-bound snapshot must not reload")
    assert manager.refresh("old", 1)["changed"] is False
    assert not old.closed and manager.pending == 0


def test_nonjob_reader_keeps_old_client_until_read_finishes(managed):
    manager, old, _ = managed
    old.read_entered, old.read_release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(1) as pool:
        reading = pool.submit(manager.active)
        assert old.read_entered.wait(2)
        assert manager.refresh("new", 1)["changed"]
        assert not old.closed
        old.read_release.set()
        assert reading.result(2)[0] == "old"
    assert old.closed == 1


def test_shutdown_during_refresh_does_not_reopen_admission(managed):
    manager, old, _ = managed
    entered, release = threading.Event(), threading.Event()
    assert manager.submit(lambda: (entered.set(), release.wait(2)))
    assert entered.wait(2)
    with ThreadPoolExecutor(1) as pool:
        refresh = pool.submit(manager.refresh, "new", 2)
        wait_paused(manager)
        manager.stop(0)
        with pytest.raises(RefreshFailure, match="PROFILER_CLOSING"):
            refresh.result(2)
        assert not manager.submit(lambda: None)
        assert manager.current is old and not old.closed
        release.set()


def test_refresh_endpoint_requires_existing_service_auth_and_valid_target(managed):
    manager, _, _ = managed
    app = FastAPI()
    app.state.shared_catalog = manager
    settings = SimpleNamespace(SERVICE_TOKEN="synthetic")
    app.dependency_overrides[settings_dep] = lambda: settings
    app.add_api_route(
        REFRESH_PATH,
        refresh_catalog,
        methods=["POST"],
        dependencies=[Depends(require_refresh_token)],
    )
    with TestClient(app) as client:
        body = {"expectedSnapshotId": "new", "drainTimeoutSeconds": 1}
        assert client.post(REFRESH_PATH, json=body).status_code == 401
        assert manager.current.snapshot == "old"
        response = client.post(
            REFRESH_PATH, json=body, headers={"Authorization": "Bearer synthetic"}
        )
        assert response.status_code == 200 and response.json()["snapshotId"] == "new"
        assert response.json()["admissionPaused"] is False
        assert (
            client.post(
                REFRESH_PATH, json=body, headers={"Authorization": "Bearer synthetic"}
            ).json()["changed"]
            is False
        )
        settings.SERVICE_TOKEN = ""
        assert client.post(REFRESH_PATH, json=body).status_code == 503


def test_same_snapshot_with_expired_lease_rebinds_instead_of_sticking(managed):
    manager, old, _ = managed
    renewed = Catalog("old")
    manager.unfinished_runs = lambda: 1  # Reacquire the exact same generation to finish old work.
    manager.factory = lambda: renewed
    old.check = lambda: (_ for _ in ()).throw(
        NoActiveCatalog("expired synthetic lease")
    )
    assert manager.refresh("old", 1)["changed"] is True
    assert manager.current is renewed and old.closed == 1
    assert manager.active()[0] == "old"


def test_same_snapshot_cannot_hide_changed_contract(managed):
    manager, old, _ = managed
    old.contract = {'encoderFingerprint':'old'}
    renewed = Catalog('old')
    renewed.contract = {'encoderFingerprint':'different'}
    manager.factory = lambda: renewed
    old.check = lambda: (_ for _ in ()).throw(NoActiveCatalog('expired lease'))
    with pytest.raises(RefreshFailure, match='TARGET_CONTRACT_MISMATCH'):
        manager.refresh('old', 1)
    assert manager.current is old and not old.closed and renewed.closed == 1


def test_cleanup_failure_does_not_leave_refresh_gate_locked(managed):
    manager, old, _ = managed
    old.close = lambda: (_ for _ in ()).throw(RuntimeError("synthetic cleanup failure"))
    assert manager.refresh("new", 1)["changed"]
    assert manager.status()["cleanupFailures"] == 1 and not manager.paused
    assert manager.refresh("new", 1)["changed"] is False


def test_failed_candidate_cleanup_cannot_prevent_admission_resume(managed):
    manager, old, new = managed
    new.close = lambda: (_ for _ in ()).throw(RuntimeError("synthetic cleanup failure"))
    with pytest.raises(RefreshFailure, match="TARGET_SNAPSHOT_MISMATCH"):
        manager.refresh("different", 1)
    assert manager.current is old and not manager.paused
    assert manager.refresh("old", 1)["changed"] is False


def health_state(manager, monkeypatch):
    from profiling import main

    monkeypatch.setattr(main, "_store_health", lambda _: {"connected": True})
    return SimpleNamespace(
        catalog=manager,
        supervisor=manager,
        shared_catalog=manager,
        embedding_status={
            "enabled": True,
            "version": manager.version,
            "matched": manager.matched,
            "total": manager.total,
        },
        integration={"modelRequired": True, "budgetGuard": "synthetic"},
    )


def render_health(state):
    from profiling import main

    return main._health_sync(state)


def assert_health_generation(body, name, count):
    assert body["catalog"] == {"active": True, "version": name, "products": count}
    assert body["embedding"] == {
        "enabled": True,
        "version": name,
        "matched": count,
        "total": count,
    }
    assert (
        body["integration"]["snapshotId"]
        == body["integration"]["catalogVersionBound"]
        == name
    )
    assert (
        isinstance(body["catalog"]["active"], bool)
        and body["store"]["connected"] is True
    )


def test_health_embedding_metadata_follows_changed_product_count(managed, monkeypatch):
    manager, old, new = managed
    old.active = lambda: ("old", [None])
    new.total = new.matched = 2
    new.active = lambda: ("new", [None, None])
    state = health_state(manager, monkeypatch)
    assert manager.refresh("new", 1)["changed"]
    assert_health_generation(render_health(state), "new", 2)
    # Both guarded runtime projections must preserve the same captured metadata.
    from integration.profiler_free_runtime import health
    from integration.profiler_runtime import integrated_health

    for renderer in (integrated_health, health):
        assert_health_generation(renderer(state), "new", 2)


def test_health_read_stays_on_one_generation_while_refresh_completes(
    managed, monkeypatch
):
    manager, old, new = managed
    entered, release = threading.Event(), threading.Event()

    def slow_read():
        entered.set()
        assert release.wait(2)
        assert not old.closed
        return "old", [None]

    old.active = slow_read
    new.total = new.matched = 2
    new.active = lambda: ("new", [None, None])
    state = health_state(manager, monkeypatch)
    with ThreadPoolExecutor(1) as pool:
        reading = pool.submit(render_health, state)
        assert entered.wait(2)
        try:
            assert manager.refresh("new", 1)["changed"]
            assert not old.closed
        finally:
            release.set()
        assert_health_generation(reading.result(2), "old", 1)
    assert old.closed == 1
    assert_health_generation(render_health(state), "new", 2)


def test_health_preserves_paused_admission_and_then_new_metadata(managed, monkeypatch):
    manager, _old, new = managed
    new.total = new.matched = 2
    new.active = lambda: ("new", [None, None])
    state = health_state(manager, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    assert manager.submit(lambda: (entered.set(), release.wait(2)))
    assert entered.wait(2)
    with ThreadPoolExecutor(1) as pool:
        updating = pool.submit(manager.refresh, "new", 2)
        wait_paused(manager)
        try:
            body = render_health(state)
            assert body["catalog"]["active"] is False
            assert body["embedding"] == {
                "enabled": True,
                "version": "old",
                "matched": 1,
                "total": 1,
            }
            assert body["integration"]["admissionPaused"] is True
        finally:
            release.set()
        assert updating.result(2)["changed"]
    assert_health_generation(render_health(state), "new", 2)


def test_health_recovers_after_same_snapshot_lease_reacquisition(managed, monkeypatch):
    manager, old, _ = managed
    renewed = Catalog("old")
    renewed.active = lambda: ("old", [None])
    state = health_state(manager, monkeypatch)
    manager.factory = lambda: renewed
    old.check = lambda: (_ for _ in ()).throw(
        NoActiveCatalog("expired synthetic lease")
    )
    assert render_health(state)["catalog"]["active"] is False
    assert manager.refresh("old", 1)["changed"]
    assert manager.current is renewed
    assert_health_generation(render_health(state), "old", 1)
