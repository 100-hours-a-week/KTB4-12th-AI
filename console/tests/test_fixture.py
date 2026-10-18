import importlib
import json

from fastapi.testclient import TestClient

from console import launch_fixture


def test_callback_calls_and_failure_modes_survive_reload(tmp_path, monkeypatch):
    path = tmp_path / "callback.json"
    monkeypatch.setenv("CONSOLE_FIXTURE_CALLBACK_STATE", str(path))
    from console import fixture_callback

    importlib.reload(fixture_callback)
    with TestClient(fixture_callback.app) as http:
        assert http.put("/fixture/mode/42/503").status_code == 200
        assert (
            http.post(
                "/api/internal/v1/recipients/42/profile", json={"synthetic": True}
            ).status_code
            == 503
        )
    importlib.reload(fixture_callback)
    with TestClient(fixture_callback.app) as http:
        assert http.get("/fixture/calls").json() == {"42": [{"synthetic": True}]}
        assert (
            http.post(
                "/api/internal/v1/recipients/42/profile", json={"synthetic": True}
            ).status_code
            == 503
        )
        assert http.put("/fixture/mode/42/200").status_code == 200
        assert (
            http.post(
                "/api/internal/v1/recipients/42/profile", json={"synthetic": True}
            ).status_code
            == 200
        )
    state = json.loads(path.read_text())
    assert len(state["calls"]["42"]) == 3 and state["modes"]["42"] == 200
    assert path.stat().st_mode & 0o777 == 0o600


def test_shutdown_uses_live_workers_not_result_ready_and_has_deadline(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *unused):
            pass

        def execute(self, query):
            assert "ai_chat.turns" in query and "RESULT_READY" not in query
            return self

        def fetchone(self):
            return (0,)

        def rollback(self):
            pass

    stats = {"running": 0, "queued": 0}

    class Http(Connection):
        def get(self, url):
            assert url.endswith("/health")
            return self

        def raise_for_status(self):
            pass

        def json(self):
            return {"supervisor": stats}

    monkeypatch.setattr(launch_fixture.psycopg, "connect", lambda *a, **k: Connection())
    monkeypatch.setattr(launch_fixture.httpx, "Client", lambda **k: Http())
    assert launch_fixture.wait_for_idle("synthetic", "http://127.0.0.1:1", timeout=0)
    stats["running"] = 1
    assert not launch_fixture.wait_for_idle(
        "synthetic", "http://127.0.0.1:1", timeout=0
    )
