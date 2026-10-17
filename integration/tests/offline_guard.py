"""Unit-only plugin: no inherited service secrets, real HTTP or real DB calls."""
import os
import re

import httpx
import pytest

for key in list(os.environ):
    if re.search(r'TOKEN|SECRET|PASSWORD|API_KEY|DATABASE_URL|^PROFILING_|^SEARCH_', key):
        os.environ.pop(key)


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Unit tests forbid real network/DB calls; use synthetic adapters')

    async def deny_async(*args, **kwargs):
        deny()

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', deny)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', deny_async)
    try:
        import psycopg
    except ImportError:
        pass
    else:
        monkeypatch.setattr(psycopg, 'connect', deny)
