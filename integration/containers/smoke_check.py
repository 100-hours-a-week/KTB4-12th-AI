"""Future image smoke client: synthetic input only, fake provider/ledger, no real API key."""

import json
import time
import uuid
from pathlib import Path

import httpx


def main():
    token = Path("/run/fixture/ai_service_token").read_text().strip()
    auth = {"Authorization": "Bearer " + token}
    if not token.startswith("synthetic-"):
        raise ValueError("Smoke client refuses real credentials")
    origin = "http://console:18800"
    console_headers = {"Host": "127.0.0.1:18811", "Origin": "http://127.0.0.1:18811"}
    with httpx.Client(trust_env=False, timeout=10) as http:
        for _ in range(100):
            try:
                if http.get(origin + "/healthz", headers=console_headers).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Synthetic Console did not become ready")
        password = Path("/run/fixture/console_admin_password").read_text().strip()
        assert password.startswith("synthetic-admin-")
        login = http.post(origin + "/api/admin/login", json={"password": password}, headers=console_headers)
        assert login.status_code == 200
        assert http.get(origin + "/api/snapshot", headers=console_headers).status_code == 200
        assert http.get("http://127.0.0.1:8001/readyz").json()["ready"]
        assert http.get("http://127.0.0.1:8000/readyz").status_code == 200
        body = {
            "recipientUserId": 42,
            "sourceVersion": 1,
            "giftPreference": "합성 컵 선물",
            "dislikedCategories": [],
            "reviews": [],
        }
        assert (
            http.put(
                "http://127.0.0.1:8002/fixture/recipients/42/profile-input",
                json=body,
                headers=auth,
            ).status_code
            == 200
        )
        assert (
            http.post(
                "http://127.0.0.1:8001/api/internal/v1/ai/profile/extract-and-pool",
                json=body,
                headers=auth,
            ).status_code
            == 202
        )
        for _ in range(100):
            data = http.get(
                "http://127.0.0.1:8002/fixture/recipients/42/chat-context", headers=auth
            ).json()
            if data.get("preparedContext"):
                break
            time.sleep(0.1)
        assert (
            data["recipientContext"]["sourceVersion"] == 1
            and data["preparedContext"]["productIds"]
        )
        cid, tid = str(uuid.uuid4()), str(uuid.uuid4())
        base = "http://127.0.0.1:7860/v1/chat/conversations/" + cid
        assert (
            http.put(
                base, headers=auth, json={"senderUserId": 7, "recipientUserId": 42}
            ).status_code
            == 201
        )
        payload = {"message": "cards"}
        assert http.put(
            base + "/turns/" + tid, headers=auth, json=payload
        ).status_code in (200, 202)
        stream = http.get(
            base + "/turns/" + tid + "/events?afterEventId=0", headers=auth
        )
        assert stream.status_code == 200 and "event: turn.finished" in stream.text
        turn = http.get(base + "/turns/" + tid, headers=auth).json()["data"]
        assert turn["status"] == "COMPLETED"
        assert (
            http.get(
                base
                + "/turns/"
                + tid
                + "/events?afterEventId="
                + str(turn["lastEventId"]),
                headers=auth,
            ).text
            == ""
        )
        assert (
            http.put(base + "/turns/" + tid, headers=auth, json=payload).json()["data"]
            == turn
        )
        assert (
            http.put(base + "/turns/" + tid + "/cancellation", headers=auth).json()[
                "data"
            ]["status"]
            == "COMPLETED"
        )
        count = http.get("http://127.0.0.1:18942/fixture/count").json()
        assert count["synthetic"] and count["providerReservations"] == 1
        assert (
            http.get(origin + "/api/snapshot", headers=console_headers).json()["services"][
                "profiling"
            ]["status"]
            == "ready"
        )
    print(
        json.dumps(
            {
                "synthetic": True,
                "paidCalls": 0,
                "realLedgerWrites": 0,
                "profileCallbackAndPreparedContext": True,
                "mockChatSseReplayRetryAndTerminalCancellation": True,
                "modelReservationsInFakeLedger": 1,
                "passed": True,
            }
        )
    )


if __name__ == "__main__":
    main()
