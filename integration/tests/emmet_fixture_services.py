"""Explicit free fixture: fake ledger + provider; no real-account I/O or external HTTP."""

import os
from pathlib import Path
from fastapi import FastAPI, HTTPException

MODE = Path(os.environ["EMMET_FIXTURE_MODE"])
app = FastAPI()
ledger = {}
wires = []


@app.post("/rpc")
def rpc(body: dict):
    args = body["args"]
    method = body["method"]
    if args.get("account") != "gift-chat-harness-2026-10-06":
        raise HTTPException(400)
    if method == "budget_summary":
        return {
            "result": {
                "limitUsd": 5,
                "committedUsd": sum(
                    a.get("cost") if a.get("cost") is not None else a["reserved"]
                    for a in ledger.values()
                ),
                "remainingUsd": 4,
                "halted": None,
                "synthetic": True,
            }
        }
    if method == "budget_reserve":
        ledger[args["ident"]] = {
            "reserved": args["amount"],
            "model": args["model"],
            "status": "reserved",
            "wireOrderVerified": True,
        }
    elif method == "budget_finish":
        ledger[args["ident"]].update(cost=args.get("cost"), status=args["status"])
    else:
        raise HTTPException(400)
    return {"result": True}


@app.get("/fixture/ledger")
def summary():
    return {"synthetic": True, "attempts": list(ledger.values())}
