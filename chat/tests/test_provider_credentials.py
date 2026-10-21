"""Synthetic request rotation exercises actual admission and budget wrappers."""
import asyncio
import json
import secrets

import httpx
import pytest

from app.diagnostics import clean, snapshot_receipt
from app.errors import APIError
from app.runtime import Runtime
from app.task_budget import BudgetHTTP
from integration.provider_key import ACTIVE_KEY, KEY_FILE, ProviderKey


@pytest.mark.asyncio
async def test_key_rotation_retains_turn_snapshot_and_budget_ledger(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    monkeypatch.delenv('OPENROUTER_API_KEY_FILE', raising=False)
    monkeypatch.setenv('OPENROUTER_MANAGED_KEY_DIR', str(tmp_path))
    source = ProviderKey(managed_directory=str(tmp_path))
    old, new = ('sk-or-v1-' + secrets.token_hex(32) for _ in range(2))
    saved = source.save(old, None)
    events, wire = [], []

    class Store:
        async def call(self, name, **kwargs):
            events.append((name, kwargs))
            if name == 'existing_turn': return None
            if name == 'start': return {'status': 'PROCESSING'}, True
            if name == 'budget_summary': return {'limitUsd': 5}
            if name in {'budget_reserve', 'budget_finish'}: return True
            raise AssertionError(name)

    store = Store(); runtime = Runtime(store, object(), 'synthetic-owner')
    auth = runtime.http._auth; await runtime.http.aclose()
    async def provider(request):
        assert events[-1][0] == 'budget_reserve'
        wire.append(request.headers['authorization'])
        assert json.loads(request.content)['provider']['max_price']
        return httpx.Response(200, json={'usage': {'cost': 0.001}})
    client = httpx.AsyncClient(base_url='https://openrouter.ai/api/v1', auth=auth, transport=httpx.MockTransport(provider), trust_env=False)
    runtime.http = BudgetHTTP(client, store, account='synthetic-budget', expected_limit=5, check_summary=True)
    runtime.catalog.http = client
    async def generation(cid, tid):
        for index in range(2):
            await runtime.http.post('/chat/completions', json={'model': 'openai/gpt-6-luna', 'messages': [], 'max_tokens': 32})
            if tid == 'first' and index == 0:
                before = list(events); source.save(new, saved['revision'])
                assert events == before  # Key storage has no ledger operations.
                assert old not in json.dumps(snapshot_receipt({'providerEcho': old}))
    runtime.run = generation
    try:
        for tid in ('first', 'second'):
            accepted = asyncio.get_running_loop().create_future(); runtime.admissions[tid] = {'model': None}
            await runtime._admit_and_run('synthetic-cid', tid, 'synthetic-message', accepted)
            assert accepted.result()['status'] == 'PROCESSING' and ACTIVE_KEY.get() is None
        assert wire == ['Bearer '+old]*2 + ['Bearer '+new]*2
        assert [name for name, _ in events].count('budget_reserve') == 4
        assert [name for name, _ in events].count('budget_finish') == 4
        assert all(value['account'] == 'synthetic-budget' for name, value in events if name.startswith('budget_'))
        assert all(value['cost'] == 0.001 for name, value in events if name == 'budget_finish')
        (tmp_path / KEY_FILE).unlink(); before = len(events)
        accepted = asyncio.get_running_loop().create_future(); runtime.admissions['missing'] = {'model': None}
        await runtime._admit_and_run('synthetic-cid', 'missing', 'synthetic-message', accepted)
        with pytest.raises(APIError) as error: accepted.result()
        assert error.value.status == 503
        assert [name for name, _ in events[before:]] == ['existing_turn'] and len(wire) == 4
        assert runtime.healthy and ACTIVE_KEY.get() is None
        assert old not in json.dumps(clean({'echo': old})) and new not in json.dumps(clean({'echo': new}))
    finally:
        await runtime.http.aclose()
