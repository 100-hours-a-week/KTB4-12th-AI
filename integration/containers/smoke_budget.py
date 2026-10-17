"""Synthetic in-memory ledger fixture; never contacts or initializes the real account."""

import hmac
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

app = FastAPI()
records = {}
TOKEN = Path(os.environ["STORE_TOKEN_FILE"]).read_text().strip()


@app.post("/rpc")
async def rpc(request: Request):
    if not hmac.compare_digest(
        request.headers.get("authorization", ""), "Bearer " + TOKEN
    ):
        raise HTTPException(401)
    data = await request.json()
    args = data["args"]
    method = data["method"]
    if args.get("account") != "gift-chat-harness-2026-10-06":
        raise HTTPException(400)
    if method == "budget_summary":
        return {
            "result": {
                "synthetic": True,
                "limitUsd": 5,
                "remainingUsd": 4,
                "committedUsd": 1,
                "attempts": len(records),
                "halted": None,
                "validationMaxAttempts": 6,
                "validationMaxReservedUsd": 0.2,
                "validationAttempts": len(records),
                "validationReservedUsd": sum(r["amount"] for r in records.values()),
            }
        }
    if method == "budget_reserve":
        if (
            len(records) >= 6
            or sum(r["amount"] for r in records.values()) + args["amount"] > 0.2
        ):
            raise HTTPException(429)
        records[args["ident"]] = {"amount": args["amount"], "cost": None}
    elif method == "budget_finish":
        records[args["ident"]]["cost"] = args.get("cost")
    else:
        raise HTTPException(400)
    return {"result": True}


@app.get("/fixture/count")
def count():
    return {"synthetic": True, "providerReservations": len(records)}
