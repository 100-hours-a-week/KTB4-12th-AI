"""Synthetic BE harness contract tests; no production BE or model calls."""

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


def harness(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "synthetic": True,
                "recipientUserId": 42,
                "sourceVersion": 1,
                "giftPreference": "synthetic",
                "dislikedCategories": [],
                "callback": None,
            }
        )
    )
    monkeypatch.setenv("LIVE_BACKEND_STATE", str(state))
    monkeypatch.setenv("AI_SERVICE_TOKEN", "synthetic-token")
    monkeypatch.setenv("SEARCH_URL", "http://synthetic.invalid")
    spec = importlib.util.spec_from_file_location(
        "synthetic_live_backend", Path(__file__).parents[1] / "live_backend.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen = []

    class Search:
        def __init__(self, *a, **kw):
            assert kw.get("headers") == {"Authorization": "Bearer synthetic-token"}, "Search requires service authentication"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, path):
            return httpx.Response(
                200,
                json={"snapshotId": "synthetic-snapshot"},
                request=httpx.Request("GET", path),
            )

        def post(self, path, json):
            seen.append(json)
            return httpx.Response(
                200,
                json={"products": [{"productId": i} for i in json["ids"]]},
                request=httpx.Request("POST", path),
            )

    monkeypatch.setattr(module.httpx, "Client", Search)
    return module, state, seen


@pytest.mark.parametrize("ids", [[], [101, 102]])
def test_empty_or_nonempty_callback_persists_and_next_context_is_valid(
    tmp_path, monkeypatch, ids
):
    module, state, seen = harness(tmp_path, monkeypatch)

    async def case():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(module.app), base_url="http://synthetic"
        ) as http:
            auth = {"Authorization": "Bearer synthetic-token"}
            body = {
                "recipientUserId": 42,
                "sourceVersion": 1,
                "profileStatus": "COMPLETED",
                "recommendedProductIds": ids,
            }
            assert (
                await http.post(
                    "/api/internal/v1/recipients/42/profile", json=body, headers=auth
                )
            ).status_code == 200
            original = state.read_bytes()
            assert (
                await http.post(
                    "/api/internal/v1/recipients/42/profile", json=body, headers=auth
                )
            ).status_code == 200
            assert state.read_bytes() == original
            data = (
                await http.get("/fixture/recipients/42/chat-context", headers=auth)
            ).json()
            assert (
                json.loads(state.read_text())["callback"]["recommendedProductIds"]
                == ids
            )
            assert ("preparedContext" in data) == bool(ids)
            assert len(seen) == (2 if ids else 0)

    asyncio.run(case())


def test_wrong_auth_recipient_stale_and_invalid_ids_rejected(tmp_path, monkeypatch):
    module, state, seen = harness(tmp_path, monkeypatch)
    original = state.read_bytes()

    async def case():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(module.app), base_url="http://synthetic"
        ) as http:
            auth = {"Authorization": "Bearer synthetic-token"}
            body = {
                "recipientUserId": 42,
                "sourceVersion": 1,
                "profileStatus": "COMPLETED",
                "recommendedProductIds": [],
            }
            path = "/api/internal/v1/recipients/42/profile"
            assert (await http.post(path, json=body)).status_code == 401
            assert (
                await http.post(path, json=body | {"recipientUserId": 43}, headers=auth)
            ).status_code == 400
            assert (
                await http.post(path, json=body | {"sourceVersion": 0}, headers=auth)
            ).status_code == 409
            for ids in [[True], [0], ["101"], [101, 101]]:
                assert (
                    await http.post(
                        path, json=body | {"recommendedProductIds": ids}, headers=auth
                    )
                ).status_code == 400

    asyncio.run(case())
    assert state.read_bytes() == original and not seen


def test_completed_empty_input_has_none_context_without_empty_pool(
    tmp_path, monkeypatch
):
    module, state, _ = harness(tmp_path, monkeypatch)
    data = json.loads(state.read_text())
    data["giftPreference"] = None
    state.write_text(json.dumps(data))

    async def case():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(module.app), base_url="http://synthetic"
        ) as http:
            auth = {"Authorization": "Bearer synthetic-token"}
            body = {
                "recipientUserId": 42,
                "sourceVersion": 1,
                "profileStatus": "COMPLETED",
                "recommendedProductIds": [],
            }
            assert (
                await http.post(
                    "/api/internal/v1/recipients/42/profile", json=body, headers=auth
                )
            ).status_code == 200
            result = (
                await http.get("/fixture/recipients/42/chat-context", headers=auth)
            ).json()
            assert result["recipientContext"] == {
                "recipientUserId": 42,
                "status": "NONE",
                "sourceVersion": 1,
                "giftPreference": None,
                "dislikedCategories": [],
            }
            assert "preparedContext" not in result

    asyncio.run(case())


def test_bound_profile_commits_matching_preferences_and_version_only_on_callback(tmp_path, monkeypatch):
    module, state, _ = harness(tmp_path, monkeypatch)

    async def case():
        auth = {"Authorization": "Bearer synthetic-token"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(module.app), base_url="http://synthetic") as http:
            path = '/fixture/recipients/42/profile-input'
            body = {"recipientUserId": 42, "sourceVersion": 1, "giftPreference": "SYNTHETIC different from seed", "dislikedCategories": [{"categoryId": 1, "categoryName": "Synthetic"}], "reviews": [{"productId": 101, "rating": 5, "reviewText": "PRIVATE_SYNTHETIC_REVIEW"}]}
            original = state.read_bytes()
            assert (await http.put(path, json=body)).status_code == 401
            assert state.read_bytes() == original
            assert (await http.put(path, json=body, headers=auth)).status_code == 200
            assert 'PRIVATE_SYNTHETIC_REVIEW' not in state.read_text()
            assert json.loads(state.read_text())['giftPreference'] == 'synthetic'
            assert (await http.put(path, json=body, headers=auth)).status_code == 200
            assert (await http.put(path, json=body | {'giftPreference': 'changed'}, headers=auth)).status_code == 409
            assert (await http.put(path, json=body | {'reviews': []}, headers=auth)).status_code == 409
            callback = {"recipientUserId": 42, "sourceVersion": 1, "profileStatus": "COMPLETED", "recommendedProductIds": [101]}
            assert (await http.post('/api/internal/v1/recipients/42/profile', json=callback, headers=auth)).status_code == 200
            context = (await http.get('/fixture/recipients/42/chat-context', headers=auth)).json()
            assert context['recipientContext']['giftPreference'] == body['giftPreference']
            assert context['recipientContext']['dislikedCategories'] == body['dislikedCategories']
            assert context['preparedContext']['productIds'] == [101]
            # A higher version may be admitted without changing the context of the completed run.
            next_body = body | {'sourceVersion': 2, 'giftPreference': None, 'dislikedCategories': [], 'reviews': []}
            assert (await http.put(path, json=next_body, headers=auth)).status_code == 200
            assert (await http.get('/fixture/recipients/42/chat-context', headers=auth)).json() == context
            assert (await http.post('/api/internal/v1/recipients/42/profile', json=callback | {'sourceVersion': 2, 'recommendedProductIds': []}, headers=auth)).status_code == 200
            latest = (await http.get('/fixture/recipients/42/chat-context', headers=auth)).json()
            assert latest['recipientContext']['sourceVersion'] == 2
            assert latest['recipientContext']['status'] == 'NONE'
            assert latest['recipientContext']['giftPreference'] is None
            assert 'preparedContext' not in latest
            committed = state.read_bytes()
            assert (await http.post('/api/internal/v1/recipients/42/profile', json=callback, headers=auth)).status_code == 409
            assert (await http.put(path, json=body, headers=auth)).status_code == 409
            assert state.read_bytes() == committed

    asyncio.run(case())


def test_legacy_completed_input_cannot_silently_bind_another_body(tmp_path, monkeypatch):
    module, state, _ = harness(tmp_path, monkeypatch)
    value = json.loads(state.read_text())
    value['callback'] = {'sourceVersion': 1, 'recommendedProductIds': []}
    state.write_text(json.dumps(value))
    original = state.read_bytes()

    async def case():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(module.app), base_url='http://synthetic') as http:
            response = await http.put('/fixture/recipients/42/profile-input', headers={'Authorization': 'Bearer synthetic-token'}, json={'recipientUserId': 42, 'sourceVersion': 1, 'giftPreference': 'different'})
            assert response.status_code == 409
            assert state.read_bytes() == original
    asyncio.run(case())


@pytest.mark.parametrize('invalid', [
    {'reviews': [{}]},
    {'dislikedCategories': [{'categoryId': i, 'categoryName': 'Synthetic'} for i in range(1,7)]},
    {'reviews': [{'productId': 101, 'rating': 6}]},
    {'dislikedCategories': [{'categoryId': 'bad', 'categoryName': 'Synthetic'}]},
])
def test_invalid_profile_never_reserves_version_before_corrected_retry(tmp_path, monkeypatch, invalid):
    module, state, _ = harness(tmp_path, monkeypatch)
    original = state.read_bytes()
    async def case():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(module.app), base_url='http://synthetic') as http:
            auth = {'Authorization': 'Bearer synthetic-token'}
            path = '/fixture/recipients/42/profile-input'
            valid = {'recipientUserId': 42, 'sourceVersion': 2, 'giftPreference': 'SYNTHETIC', 'dislikedCategories': [], 'reviews': []}
            assert (await http.put(path,json=valid|invalid,headers=auth)).status_code == 422
            assert state.read_bytes() == original
            assert (await http.put(path,json=valid,headers=auth)).status_code == 200
    asyncio.run(case())
