import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import psycopg
import pytest
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


def test_fresh_fixture_uses_current_migrations_and_application_roles(monkeypatch):
    statements, commands = [], []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *unused):
            pass

        def execute(self, query):
            statements.append(query.as_string(None) if hasattr(query, 'as_string') else query)

    monkeypatch.setattr(launch_fixture.psycopg, 'connect', lambda *a, **k: Connection())
    launch_fixture.initialize_database('postgresql://fixture_owner@127.0.0.1:1/emmet4930_console',
        Path('/synthetic/python'), lambda command, cwd, extra: commands.append((command, extra)))
    roles = '\n'.join(statements)
    assert '"ai_profiler_service"' in roles and '"ai_console_service"' in roles
    assert '"ai_app"' not in roles
    assert commands[0][0][-3:] == ['alembic', 'upgrade', '0011']
    assert commands[0][1]['PROFILING_APP_DB_ROLE'] == 'ai_profiler_service'
    assert commands[1][0][-3:] == ['integration.migrate_single_server', '--database', 'emmet4930_console']
    assert commands[1][1]['INTEGRATION_SYNTHETIC_ONLY'] == '1'


@pytest.mark.parametrize('fault', [None, 'legacy', 'missing_review'])
def test_resume_preflight_is_read_only_and_rejects_unprepared_state(monkeypatch, fault):
    statements = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *unused):
            pass

        def execute(self, query):
            statements.append(query)
            if fault == 'missing_review' and 'turn_reviews' in query:
                raise psycopg.errors.UndefinedTable('synthetic missing review table')
            return self

        def fetchone(self):
            if 'count(*)' in statements[-1]:
                return (1,)
            return ('emmet4930_console', 'emmet4930_console', 'single-server/1', '0008' if fault == 'legacy' else '0011')

    def connect(url, **kwargs):
        assert kwargs['options'] == '-c default_transaction_read_only=on'
        return Connection()

    monkeypatch.setattr(launch_fixture.psycopg, 'connect', connect)
    if fault:
        with pytest.raises(RuntimeError, match='existing state was preserved'):
            launch_fixture.check_database('synthetic')
    else:
        launch_fixture.check_database('synthetic')
    assert all(query.strip().startswith('SELECT') for query in statements)


def test_startup_failure_stops_owned_children_and_pg_without_idle_probe(tmp_path, monkeypatch):
    root, envs, assets, pg = (tmp_path/name for name in ('source', 'envs', 'assets', 'pg-bin'))
    for name in ('chat', 'profiling', 'product-search', 'console'):
        path = envs/name/'.venv/bin/python'
        path.parent.mkdir(parents=True)
        path.touch()
    for name in ('models', 'node_modules'):
        (assets/name).mkdir(parents=True)
    (assets/'runtime.mjs').touch()
    (root/'product-search/embedding').mkdir(parents=True)
    (root/'product-search/embedding/model.lock.json').write_text('{"files": []}')
    pg.mkdir()
    for name in ('initdb', 'pg_ctl', 'pg_config'):
        (pg/name).touch()
    share = tmp_path/'pg-share'
    (share/'extension').mkdir(parents=True)
    (share/'extension/vector.control').touch()
    commands, children = [], []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *unused):
            pass

        def execute(self, query):
            return self

        def get(self, *args, **kwargs):
            raise httpx.ConnectError('synthetic startup failure')

    class Child:
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout):
            assert self.stopped

    def popen(*args, **kwargs):
        child = Child()
        children.append(child)
        return child

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stderr='', stdout='')

    monkeypatch.setattr(launch_fixture, 'ROOT', root)
    monkeypatch.setattr('console.admin_auth.AdminAuth.credential', lambda self: 'synthetic')
    monkeypatch.setattr(launch_fixture.subprocess, 'run', run)
    monkeypatch.setattr(launch_fixture.subprocess, 'check_output', lambda *a, **k: str(share))
    monkeypatch.setattr(launch_fixture.subprocess, 'Popen', popen)
    monkeypatch.setattr(launch_fixture.shutil, 'which', lambda name: '/synthetic/node')
    monkeypatch.setattr(launch_fixture.psycopg, 'connect', lambda *a, **k: Connection())
    monkeypatch.setattr(launch_fixture.httpx, 'Client', lambda **kwargs: Connection())
    monkeypatch.setattr(launch_fixture.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(launch_fixture.time, 'sleep', lambda seconds: None)
    numbers = iter(range(50000, 50010))
    monkeypatch.setattr(launch_fixture, 'port', lambda: next(numbers))
    monkeypatch.setattr(launch_fixture, 'initialize_database', lambda *args: None)
    monkeypatch.setattr(launch_fixture, 'check_database', lambda *args: None)

    def idle(*args):
        pytest.fail('No request was admitted; failed startup must not query an unavailable app')

    monkeypatch.setattr(launch_fixture, 'wait_for_idle', idle)
    monkeypatch.setattr(sys, 'argv', ['launch_fixture', '--pg-bin', str(pg), '--embedding-assets', str(assets),
        '--environment-root', str(envs), '--state-dir', str(tmp_path/'state'), '--output', str(tmp_path/'result.json')])
    with pytest.raises(RuntimeError, match='Fixture readiness failed: search'):
        launch_fixture.main()
    assert children and all(child.stopped for child in children)
    assert any(Path(command[0]).name == 'pg_ctl' and command[-1] == 'stop' for command in commands)
    assert not (tmp_path/'result.json').exists()


def test_shared_fixture_export_uses_current_pg_sync(monkeypatch):
    from integration.containers.smoke_init import stage_catalog
    observed = {}

    def sync(runtime, url, fetch, *, apply):
        export, categories = fetch()
        observed.update(runtime=str(runtime), url=url, export=export, categories=categories, apply=apply)
        return {'applied': True}

    monkeypatch.setitem(sys.modules, 'search.complete_export', SimpleNamespace(digest=lambda *args: 'f'*64))
    monkeypatch.setitem(sys.modules, 'tools.sync_postgres', SimpleNamespace(sync=sync))
    result = stage_catalog(Path('/synthetic/runtime'), 'synthetic-owner-url',
                           launch_fixture.ROOT/'integration/tests/fixture.json')
    assert result['applied'] and observed['apply']
    assert observed['export']['data']['snapshot']['complete'] is True
    assert observed['export']['data']['snapshot']['productCount'] == 3
    assert [p['productId'] for p in observed['export']['data']['products']] == [101, 102, 103]


@pytest.mark.parametrize('fault', [None, 'port', 'role', 'live', 'public_file'])
def test_checker_reads_only_its_marked_private_fixture(tmp_path, fault):
    from console.check_fixture import fixture_settings
    marker = {'dbPort': 50123, 'urls': {'search': 'http://127.0.0.1:50124'}}
    settings = {'localFixtureProfilerFree': True,
                'database': 'postgresql://ai_console_service@127.0.0.1:50123/emmet4930_console',
                'targets': {'search': marker['urls']['search']}, 'tokens': {'search': 'synthetic-checker-token'}}
    if fault == 'port':
        settings['database'] = settings['database'].replace(':50123/', ':50125/')
    elif fault == 'role':
        settings['database'] = settings['database'].replace('ai_console_service@', 'fixture_owner@')
    elif fault == 'live':
        settings['liveModel'] = True
    path = tmp_path/'targets.json'
    path.write_text(json.dumps(settings))
    path.chmod(0o644 if fault == 'public_file' else 0o600)
    if fault:
        with pytest.raises(ValueError):
            fixture_settings(tmp_path, marker)
    else:
        assert fixture_settings(tmp_path, marker) == settings
