"""Free synthetic request/retry check for a marked local fixture; no provider API."""

import argparse
import hashlib
import json
import time
import uuid
from pathlib import Path

import httpx
import psycopg

from console.server import target_url
from console.admin_auth import login_client
from integration.provider_key import read_file


def fixture_settings(scratch, marker):
    settings = json.loads(read_file(scratch/'targets.json', private=True, limit=16384))
    db = psycopg.conninfo.conninfo_to_dict(settings['database'])
    if (settings.get('localFixtureProfilerFree') is not True or settings.get('liveModel')
            or target_url(settings['targets']['search']) != target_url(marker['urls']['search'])
            or db.get('host') != '127.0.0.1' or db.get('port') != str(marker['dbPort'])
            or db.get('user') != 'ai_console_service' or not db.get('dbname', '').startswith('emmet4930_')
            or not settings.get('tokens', {}).get('search')):
        raise ValueError('Marked synthetic fixture configuration required')
    return settings


def check(runtime, previous=None, *, password=None):
    manifest = json.loads(runtime.read_text())
    scratch = Path(manifest["scratch"])
    marker = json.loads((scratch / "fixture-state.json").read_text())
    if marker["format"] != "synthetic-console-fixture/1":
        raise ValueError("Only marked synthetic fixtures are supported")
    settings = fixture_settings(scratch, marker)
    base = target_url(manifest["url"])
    callback = target_url(marker["urls"]["callback"])
    search = target_url(marker["urls"]["search"])
    search_headers = {'Authorization': 'Bearer ' + settings['tokens']['search']}
    headers = {"Origin": base}
    tests = []
    with httpx.Client(timeout=25, trust_env=False, follow_redirects=False) as http:
        headers = login_client(http, base, password=password)

        def get_turn(tid):
            response = http.get(base + "/api/chat/" + tid)
            response.raise_for_status()
            return response.json()["data"]

        def await_turn(tid, status):
            for _ in range(100):
                if get_turn(tid)["status"] == status:
                    return
                time.sleep(0.1)
            raise AssertionError("Expected chat status " + status)

        def profile(body, expected):
            response = http.post(base + "/api/profile", json=body, headers=headers)
            response.raise_for_status()
            assert response.json()["httpStatus"] in {200, 202}
            path = f"/api/profile/{body['recipientUserId']}/{body['sourceVersion']}"
            for _ in range(100):
                response = http.get(base + path)
                response.raise_for_status()
                data = response.json()["data"]
                if data is not None and data["status"] == expected:
                    return
                time.sleep(0.1)
            raise AssertionError("Expected profile status " + expected)

        if previous:
            old = json.loads(previous.read_text())
            # The console deliberately keeps its browser/admission history in memory.
            # Confirm preserved service records, then re-admit the exact old UUID/body.
            with psycopg.connect(
                settings['database'], options="-c default_transaction_read_only=on"
            ) as connection:
                for tid, state in old["turns"].items():
                    row = connection.execute(
                        "SELECT state->>'status' FROM ai_chat.turns WHERE id=%s", (tid,)
                    ).fetchone()
                    assert row and row[0] == state
            current_calls = http.get(callback + "/fixture/calls").json()
            assert all(
                current_calls[k][: len(v)] == v for k, v in old["callbackCalls"].items()
            )
            current_modes = json.loads((scratch / "callback-state.json").read_text())[
                "modes"
            ]
            assert all(current_modes[k] == v for k, v in old["callbackModes"].items())
            response = http.post(
                base + "/api/chat", json=old["retryBody"], headers=headers
            )
            assert response.json()["httpStatus"] == 200
            assert get_turn(old["retryBody"]["turn_id"])["status"] == "COMPLETED"
            profile(old["profileBody"], "DELIVERED")
            tests.append(
                "restart retained terminal turns, callback calls/modes and same-body idempotency"
            )
        body = {
            k: str(uuid.uuid4()) for k in ("request_id", "conversation_id", "turn_id")
        }
        body["message"] = "/mock reconnect"
        assert (
            http.post(base + "/api/chat", json=body, headers=headers).json()[
                "httpStatus"
            ]
            == 202
        )
        await_turn(body["turn_id"], "COMPLETED")
        stream = http.get(
            base + "/api/chat/" + body["turn_id"] + "/events?afterEventId=0"
        )
        assert "turn.finished" in stream.text and "answer.delta" in stream.text
        assert (
            http.post(base + "/api/chat", json=body, headers=headers).json()[
                "httpStatus"
            ]
            == 200
        )
        tests.append("stored SSE completion/replay and actual same-body upstream retry")
        turns = {body["turn_id"]: "COMPLETED"}
        for scenario, expected in (
            ("/mock failed", "FAILED"),
            ("/mock slow", "CANCELLED"),
        ):
            trial = {
                k: str(uuid.uuid4())
                for k in ("request_id", "conversation_id", "turn_id")
            }
            trial["message"] = scenario
            http.post(
                base + "/api/chat", json=trial, headers=headers
            ).raise_for_status()
            if expected == "CANCELLED":
                http.put(
                    base + "/api/chat/" + trial["turn_id"] + "/cancellation",
                    json={},
                    headers=headers,
                ).raise_for_status()
            await_turn(trial["turn_id"], expected)
            turns[trial["turn_id"]] = expected
        tests.append("actual mock failure and cancellation stay terminal")
        response = http.post(
            search + "/v1/search",
            json={"query": "SYNTHETIC", "mode": "hybrid", "limit": 3},
            headers=search_headers,
        )
        response.raise_for_status()
        result = response.json()
        assert {h["productId"] for h in result["hits"]} == {101, 102, 103}
        assert result["snapshotId"]
        response = http.post(
            search + "/v1/products",
            json={"ids": [101], "snapshotId": "wrong-synthetic-version"},
            headers=search_headers,
        )
        assert response.status_code == 409
        assert (
            http.post(
                search + "/v1/search", json={"query": "SYNTHETIC", "limit": 0}, headers=search_headers
            ).status_code
            == 422
        )
        assert http.get(search + "/readyz").status_code == 200
        tests.append(
            "real local hybrid encoder/search, IDs and snapshot409/validation422 recovery"
        )
        recipient = int(uuid.uuid4().int % 1000000000) + 100000
        profile_body = {
            "recipientUserId": recipient,
            "sourceVersion": 1,
            "giftPreference": "SYNTHETIC portable gifts",
        }
        http.put(callback + f"/fixture/mode/{recipient}/400").raise_for_status()
        profile(profile_body, "FAILED")
        http.put(callback + f"/fixture/mode/{recipient}/200").raise_for_status()
        profile(profile_body, "DELIVERED")
        # Keep an unused failure mode too, proving modes persist independently of calls.
        http.put(callback + f"/fixture/mode/{recipient + 1}/503").raise_for_status()
        tests.append(
            "actual free profiler callback400 failure, same-body retry and successful delivery"
        )
        calls = http.get(callback + "/fixture/calls").json()
        modes = json.loads((scratch / "callback-state.json").read_text())["modes"]
        critical = (
            "product-search/search/reloading.py",
            "profiling/src/profiling/stores.py",
            "console/server.py",
        )
        root = Path(marker["sourceRoot"])
        assert all(
            hashlib.sha256((root / p).read_bytes()).hexdigest()
            == manifest["sourceHashes"][p]
            for p in critical
        )
        tests.append(
            "running instance startup source hashes match latest two fixes and console"
        )
    return {
        "passed": True,
        "synthetic": True,
        "paidCalls": 0,
        "tests": tests,
        "turns": turns,
        "retryBody": body,
        "profileBody": profile_body,
        "callbackCalls": calls,
        "callbackModes": modes,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = check(args.runtime, args.previous)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {k: result[k] for k in ("passed", "synthetic", "paidCalls", "tests")}
        )
    )


if __name__ == "__main__":
    main()
