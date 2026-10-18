"""Local validation bridge to the existing NUC ledger, never a new allowance.

Only three cost methods and the existing task account cross the established SSH
connection. Credentials travel in process memory/stdin, never arguments/logs.
The durable local run limit is stricter than the central cumulative five dollars.
"""

import hmac
import json
import math
import os
import shlex
import sqlite3
import subprocess
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MAX_ATTEMPTS = int(os.getenv("LIVE_VALIDATION_MAX_ATTEMPTS", "6"))
MAX_RESERVED_USD = float(os.getenv("LIVE_VALIDATION_MAX_RESERVED_USD", "0.20"))
if not 1 <= MAX_ATTEMPTS <= 100 or not 0 < MAX_RESERVED_USD <= 1:
    raise ValueError("Validation guard configuration outside supported bounds")
ACCOUNT = "gift-chat-harness-2026-10-06"
TOKEN = os.environ["STORE_TOKEN"]
PATH = Path(os.environ["LIVE_VALIDATION_BUDGET_DB"])
app = FastAPI(docs_url=None, redoc_url=None)
with sqlite3.connect(PATH) as db:
    db.execute(
        "CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,amount REAL NOT NULL,model TEXT NOT NULL,ack INTEGER NOT NULL DEFAULT 0)"
    )
PATH.chmod(0o600)

REMOTE = """import sys,json,urllib.request
x=json.load(sys.stdin)
r=urllib.request.Request('http://127.0.0.1:8941/rpc',data=json.dumps({'method':x['method'],'args':x['args']}).encode(),headers={'Authorization':'Bearer '+x['token'],'Content-Type':'application/json'},method='POST')
try:
 with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r,timeout=8) as response:d=json.load(response)
 print(json.dumps(d))
except Exception as e:print(json.dumps({'errorClass':type(e).__name__,'status':getattr(e,'code',503)}))
"""


def remote(method, args):
    proc = subprocess.run(
        [
            "/usr/bin/ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "nuc",
            "python3 -c " + shlex.quote(REMOTE),
        ],
        input=json.dumps({"token": TOKEN, "method": method, "args": args}),
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if proc.returncode:
        raise ValueError("Existing SSH cost connection unavailable")
    data = json.loads(proc.stdout)
    if "result" not in data:
        raise ValueError("Central cost operation not acknowledged")
    return data["result"]


@app.post("/rpc")
async def rpc(request: Request):
    if request.headers.get("origin") or not hmac.compare_digest(
        request.headers.get("authorization", ""), "Bearer " + TOKEN
    ):
        return JSONResponse({"message": "Unauthorized cost request"}, status_code=401)
    raw = await request.body()
    if len(raw) > 8192:
        return JSONResponse({"message": "Cost body too large"}, status_code=413)
    try:
        data = json.loads(raw)
        method, args = data["method"], data["args"]
        if (
            method not in {"budget_summary", "budget_reserve", "budget_finish"}
            or args.get("account") != ACCOUNT
        ):
            raise ValueError("Unsupported account or operation")
        if method == "budget_reserve":
            if args.get("model") != "openai/gpt-6-luna":
                raise ValueError("Validation model was not verified")
            amount = args["amount"]
            if (
                type(amount) not in (int, float)
                or not math.isfinite(amount)
                or not 0 < amount <= 0.10
            ):
                raise ValueError("Per-attempt validation reservation exceeds ten cents")
            with sqlite3.connect(PATH, timeout=5) as db:
                db.execute("BEGIN IMMEDIATE")
                prior = db.execute(
                    "SELECT amount,model,ack FROM attempts WHERE id=?", (args["ident"],)
                ).fetchone()
                if prior:
                    if prior != (amount, args["model"], 1):
                        raise ValueError(
                            "Uncertain or conflicting reservation remains retained"
                        )
                    return {"result": True}
                count, total = db.execute(
                    "SELECT count(*),coalesce(sum(amount),0) FROM attempts"
                ).fetchone()
                if count >= MAX_ATTEMPTS or total + amount > MAX_RESERVED_USD + 1e-9:
                    return JSONResponse(
                        {"message": "Validation run reservation cap reached"},
                        status_code=429,
                    )
                db.execute(
                    "INSERT INTO attempts(id,amount,model) VALUES(?,?,?)",
                    (args["ident"], amount, args["model"]),
                )
        result = await __import__("asyncio").to_thread(remote, method, args)
        if method == "budget_reserve":
            if result is not True:
                raise ValueError("Reservation not acknowledged; paid request forbidden")
            with sqlite3.connect(PATH) as db:
                db.execute("UPDATE attempts SET ack=1 WHERE id=?", (args["ident"],))
        if method == "budget_summary":
            if result.get("limitUsd") != 5:
                raise ValueError("Existing cumulative five-dollar ledger not verified")
            with sqlite3.connect(PATH) as db:
                count, total = db.execute(
                    "SELECT count(*),coalesce(sum(amount),0) FROM attempts"
                ).fetchone()
            result = {
                **result,
                "validationAttempts": count,
                "validationReservedUsd": total,
                "validationMaxAttempts": MAX_ATTEMPTS,
                "validationMaxReservedUsd": MAX_RESERVED_USD,
            }
        return {"result": result}
    except (
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        subprocess.SubprocessError,
        OSError,
    ):
        return JSONResponse(
            {
                "message": "Shared budget unavailable or not acknowledged; no paid send permitted"
            },
            status_code=503,
        )
