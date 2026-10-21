"""Few synthetic real-model requests; normal Chat routes and BE-contract harness.

This command spends from the already-approved shared account. It never uses the
protocol /test path and never substitutes mock output after a model failure.
"""

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx
import psycopg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--phase", choices=["search", "profile", "personalized"], required=True
    )
    args = parser.parse_args()
    runtime = json.loads((args.run_dir / "runtime.json").read_text())
    if (
        runtime["mode"] != "LIVE"
        or runtime["model"] != "openai/gpt-6-luna"
        or runtime["catalogProducts"] != 4231
    ):
        raise ValueError("Verified live isolated full-catalog runtime required")
    base = runtime["url"]
    headers = {"Origin": base}
    dburl = f"postgresql://fixture_owner@127.0.0.1:{runtime['databasePort']}/{runtime.get('databaseName','emmet4930_console')}"
    result = {
        "phase": args.phase,
        "syntheticInputOnly": True,
        "model": runtime["model"],
        "passed": False,
        "normalRoutesOnly": True,
    }
    with httpx.Client(timeout=200, trust_env=False, follow_redirects=False) as http:
        cfg = http.get(base + "/api/config")
        cfg.raise_for_status()
        assert cfg.json()["paidCallsEnabled"] is True
        if args.phase == "profile":
            seed = json.loads((args.run_dir / "backend-state.json").read_text())
            body = {
                k: seed[k]
                for k in (
                    "recipientUserId",
                    "sourceVersion",
                    "giftPreference",
                    "dislikedCategories",
                )
            }
            body["reviews"] = []
            response = http.post(base + "/api/profile", json=body, headers=headers)
            response.raise_for_status()
            assert response.json()["httpStatus"] in (200, 202)
            for _ in range(180):
                data = http.get(
                    base + f"/api/profile/{runtime['recipientUserId']}/1"
                ).json()["data"]
                if data and data["status"] in ("DELIVERED", "FAILED"):
                    break
                time.sleep(0.5)
            assert data and data["status"] == "DELIVERED", data
            assert data["tagStatus"] == "ok", data
            saved = json.loads((args.run_dir / "backend-state.json").read_text())
            assert (
                saved["callback"]["sourceVersion"] == 1
                and saved["callback"]["recommendedProductIds"]
            )
            with psycopg.connect(
                dburl, options="-c default_transaction_read_only=on"
            ) as db:
                row = db.execute(
                    "SELECT catalog_version_id,tag_status,llm_cost_usd,prompt_version FROM ai_profile.profile_runs WHERE recipient_user_id=%s AND source_version=1",
                    (runtime["recipientUserId"],),
                ).fetchone()
                assert str(row[0]) == runtime["catalogVersion"]
                assert row[1] == "ok" and "openai/gpt-6-luna" in row[3]
            assert saved["snapshotId"] == runtime["searchSnapshotId"]
            result.update(
                passed=True,
                status="DELIVERED",
                tagStatus=row[1],
                reportedCostUsd=float(row[2]) if row[2] is not None else None,
                catalogVersion=str(row[0]),
                searchSnapshotId=saved["snapshotId"],
                callbackPersisted=True,
                candidateCount=len(saved["callback"]["recommendedProductIds"]),
            )
        else:
            prior_path = args.run_dir / "search-private.json"
            if args.phase == "personalized":
                prior = json.loads(prior_path.read_text())
                cid = prior["body"]["conversation_id"]
                assert json.loads((args.run_dir / "backend-state.json").read_text())[
                    "callback"
                ]
                message = "합성 검증: 방금 반영된 수신자 취향과 준비된 후보 안에서 3만원 이하 실용적인 선물 하나를 추천해 주세요."
            else:
                cid = str(uuid.uuid4())
                message = "합성 검증입니다. 상품 검색 도구를 사용해 '텀블러' 후보를 확인하고 3만원 이하 선물 한 개를 상품 카드와 함께 추천해 주세요. 실제 구매는 하지 않습니다."
            body = {
                "request_id": str(uuid.uuid4()),
                "conversation_id": cid,
                "turn_id": str(uuid.uuid4()),
                "message": message,
                "recipientUserId": runtime["recipientUserId"],
            }
            (args.run_dir / (args.phase + "-private.json")).write_text(
                json.dumps(
                    {"synthetic": True, "body": body}, ensure_ascii=False, indent=2
                )
                + "\n"
            )
            receipt = http.post(base + "/api/chat", json=body, headers=headers)
            receipt.raise_for_status()
            receipt = receipt.json()
            assert receipt["httpStatus"] == 202, receipt
            assert all(
                "/test/" not in e["path"] for e in receipt["transport"]["exchanges"]
            )
            public = []
            kind = None
            with http.stream(
                "GET", base + "/api/chat/" + body["turn_id"] + "/events?afterEventId=0"
            ) as stream:
                stream.raise_for_status()
                for line in stream.iter_lines():
                    if line.startswith("event:"):
                        kind = line[6:].strip()
                    elif line.startswith("data:") and kind != "console.http":
                        public.append({"event": kind, "data": json.loads(line[5:])})
            state = http.get(base + "/api/chat/" + body["turn_id"]).json()["data"]
            (args.run_dir / (args.phase + "-answer.private.json")).write_text(
                json.dumps(
                    {"synthetic": True, "state": state, "events": public},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            assert state["status"] == "COMPLETED", state.get("error")
            ids = [
                p["productId"]
                for block in state["blocks"]
                if block["type"] == "cards"
                for p in block["products"]
            ]
            assert ids, "Actual answer contained no cards"
            assert any(e["event"] == "answer.delta" for e in public) and any(
                e["event"] == "turn.finished" for e in public
            )
            with psycopg.connect(
                dburl, options="-c default_transaction_read_only=on"
            ) as db:
                context, recipient, prepared = db.execute(
                    "SELECT context,recipient_context,prepared_context FROM ai_chat.turns WHERE id=%s",
                    (body["turn_id"],),
                ).fetchone()
                assert (
                    context["model"] == "openai/gpt-6-luna"
                    and context["modelRequestCount"] >= 1
                )
                assert context["snapshotId"] == runtime["searchSnapshotId"]
                if args.phase == "search":
                    assert context["toolBatches"] >= 1
                for ident in ids:
                    assert db.execute(
                        "SELECT 1 FROM ai_catalog.products WHERE id=%s AND package_id=(SELECT package_id FROM ai_catalog.catalog_versions WHERE is_active)",
                        (ident,),
                    ).fetchone()
            if args.phase == "personalized":
                saved = json.loads((args.run_dir / "backend-state.json").read_text())
                assert (
                    recipient["status"] == "AVAILABLE"
                    and recipient["sourceVersion"] == 1
                    and recipient["recipientUserId"] == runtime["recipientUserId"]
                )
                assert prepared["snapshotId"] == runtime["searchSnapshotId"] and set(
                    ids
                ) <= set(saved["callback"]["recommendedProductIds"])
                result.update(
                    callbackToRecipientContextVerified=True,
                    preparedCandidatesVerified=True,
                )
            replay = http.post(base + "/api/chat", json=body, headers=headers)
            replay.raise_for_status()
            assert replay.json()["httpStatus"] == 200
            cancel = http.put(
                base + "/api/chat/" + body["turn_id"] + "/cancellation",
                json={},
                headers=headers,
            )
            cancel.raise_for_status()
            assert cancel.json()["body"]["data"]["status"] == "COMPLETED"
            result.update(
                passed=True,
                storedStatus=state["status"],
                productIds=ids,
                modelRequests=context["modelRequestCount"],
                toolBatches=context["toolBatches"],
                reportedUsage=context.get("usage"),
                eventCount=len(public),
                sameBodyRetryNoNewTurn=True,
                normalTerminalCancellationIdempotent=True,
                searchSnapshotId=context["snapshotId"],
            )
    (args.run_dir / (args.phase + "-result.json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
