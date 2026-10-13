"""Exercise the actual lifespan with mock generation and a synthetic DB adapter."""
import asyncio
import uuid

import httpx

from app import local_store, main
from test_hf_integration import FixtureStore


class BootstrapStore(FixtureStore):
    async def call(self, method, **args):
        if method == "health":
            return {"database": "postgresql"}
        if method == "lease":
            if args.get("inspect"):
                return {"owner": None, "active": False}
            return True
        return await super().call(method, **args)

    async def close(self):
        pass


async def test_mock_bootstrap_auth_readiness_and_fixture_without_external_calls(monkeypatch):
    store = BootstrapStore()
    monkeypatch.setenv("STORE_MODE", "postgres")
    monkeypatch.setenv("DATABASE_URL", "synthetic-only-no-connection")
    monkeypatch.setenv("CHAT_RUNTIME_MODE", "mock")
    monkeypatch.setattr(local_store, "LocalStore", lambda url: store)
    for name in ["OPENROUTER_API_KEY", "SEARCH_TOKEN"]:
        monkeypatch.delenv(name, raising=False)
    async with main.lifespan(main.app):
        for _ in range(100):
            if main.app.state.runtime.healthy:
                break
            await asyncio.sleep(.01)
        async with httpx.AsyncClient(base_url="https://test", transport=httpx.ASGITransport(app=main.app)) as client:
            health = await client.get("/healthz")
            assert health.status_code == 200 and health.json()["mode"] == "mock"
            assert (await client.get("/readyz")).status_code == 401
            auth = {"Authorization": "Bearer " + main.TOKEN}
            ready = await client.get("/readyz", headers=auth)
            assert ready.status_code == 200 and ready.json()["model"] == "mock-v1"
            cid, tid = str(uuid.uuid4()), str(uuid.uuid4())
            root = main.TEST_BASE + "/" + cid
            assert (await client.put(root, json={"senderUserId": 7, "recipientUserId": 42}, headers=auth)).status_code == 201
            assert (await client.put(root + "/turns/" + tid, json={"message": "/mock text"}, headers=auth)).status_code == 202
            await asyncio.gather(*main.app.state.runtime.jobs.values())
            final = (await client.get(root + "/turns/" + tid, headers=auth)).json()["data"]
            assert final["status"] == "COMPLETED"
            replay = await client.get(root + "/turns/" + tid + "/events", headers=auth)
            assert "event: turn.finished" in replay.text
