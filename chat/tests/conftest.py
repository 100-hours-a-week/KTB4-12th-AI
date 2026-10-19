"""Synthetic-only tests: strip inherited secrets and deny real HTTP transports."""
import os
import re

import httpx
import pytest

for name in list(os.environ):
    if re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|DATABASE_URL|STORE_URL|SEARCH_URL|BRIDGE_URL|^CHAT_|^CEREBRAS_|^OPENROUTER_", name):
        os.environ.pop(name)
os.environ.update(
    AI_SERVICE_TOKEN="test-service-token",
    SESSION_SECRET="local-test-session-secret",
    QA_ADMIN_PASSWORD="local-test-admin",
    DEMO_ACCESS_TOKEN="test-team-token",
    OPENROUTER_API_KEY="test-only-not-a-real-key",
    SEARCH_TOKEN="test-search-token",
    STORE_TOKEN="test-store-token",
    BUDGET_ACCOUNT="synthetic-budget",
    BUDGET_EXPECTED_LIMIT_USD="5",
)


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    async def deny_async(*args, **kwargs):
        raise AssertionError("Real outbound HTTP is forbidden in synthetic tests")

    def deny_sync(*args, **kwargs):
        raise AssertionError("Real outbound HTTP is forbidden in synthetic tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_sync)
