"""Native sockets to owned loopback fake ledger only; never container/image evidence."""

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from integration.containers.relay import ACCOUNT, BudgetRelay
from integration.shared_budget import SharedBudget


def main():
    records = {}
    state = {"halted": None}
    calls = []
    provider_sends = 0
    token = "synthetic-loopback-only"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer " + token
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(data["method"])
            assert data["args"]["account"] == ACCOUNT
            method, args = data["method"], data["args"]
            if method == "budget_summary":
                result = {
                    "limitUsd": 5,
                    "halted": state["halted"],
                    "remainingUsd": 4,
                    "validationMaxAttempts": 6,
                    "validationMaxReservedUsd": 0.2,
                    "validationAttempts": len(records),
                    "validationReservedUsd": sum(r["amount"] for r in records.values()),
                }
            elif method == "budget_reserve":
                records[args["ident"]] = {
                    "amount": args["amount"],
                    "cost": None,
                    "status": "reserved",
                }
                result = True
            elif method == "budget_finish":
                records[args["ident"]].update(cost=args["cost"], status=args["status"])
                result = True
            else:
                raise AssertionError("Unexpected ledger method")
            raw = json.dumps({"result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    fake = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=fake.serve_forever, daemon=True)
    thread.start()
    relay = BudgetRelay(
        f"http://127.0.0.1:{fake.server_port}", token, 6, 0.2, synthetic=True
    )
    budget = None
    try:
        relay.start()
        budget = SharedBudget(relay.url, token)
        _, ident = budget.reserve(
            {"model": "openai/gpt-6-luna", "max_tokens": 1200, "messages": []}
        )
        provider_sends += (
            1  # Synthetic send marker only; there is no provider client in this script.
        )
        state["halted"] = "synthetic-stop"
        budget.finish(ident, None)
        assert (
            records[ident]["status"] == "unknown"
            and records[ident]["cost"] is None
            and records[ident]["amount"] > 0
        )
        try:
            budget.reserve(
                {"model": "openai/gpt-6-luna", "max_tokens": 1200, "messages": []}
            )
        except httpx.HTTPStatusError:
            pass
        else:
            raise AssertionError("Halted budget admitted a send")
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "chat"))
        from app.budget_ledger import BudgetLedger
        from app.errors import APIError

        async def blocked_chat():
            chat = BudgetLedger(relay.url, token)
            try:
                try:
                    await chat.call(
                        "budget_reserve",
                        account=ACCOUNT,
                        ident="synthetic-chat-blocked",
                        amount=0.01,
                        model="openai/gpt-6-luna",
                    )
                except APIError as exc:
                    assert exc.status == 503
                else:
                    raise AssertionError("Halted Chat ledger admitted a reservation")
            finally:
                await chat.close()

        asyncio.run(blocked_chat())
        assert len(records) == 1 and provider_sends == 1
        with httpx.Client(trust_env=False, timeout=2) as http:
            before = len(calls)
            assert (
                http.post(
                    relay.url + "/rpc",
                    json={"method": "budget_summary", "args": {"account": ACCOUNT}},
                ).status_code
                == 503
            )
            assert (
                http.post(
                    relay.url + "/rpc",
                    headers={"Authorization": "Bearer " + token},
                    json={"method": "start", "args": {"account": ACCOUNT}},
                ).status_code
                == 503
            )
            assert len(calls) == before
        result = {
            "passed": True,
            "nativeLoopbackOnly": True,
            "imagesBuilt": False,
            "containersStarted": False,
            "paidCalls": 0,
            "realLedgerWrites": 0,
            "checks": [
                "real HTTP reserve through dedicated relay",
                "unknown settlement allowed after halt and reservation retained",
                "halt prevents next synthetic send",
                "actual Chat BudgetLedger blocks against halted loopback relay",
                "unauthorized and non-cost RPC never forwarded",
            ],
            "fakeReservations": len(records),
            "fakeSendMarkers": provider_sends,
        }
        print(json.dumps(result))
    finally:
        if budget:
            budget.close()
        relay.close()
        fake.shutdown()
        fake.server_close()
        thread.join(timeout=3)


if __name__ == "__main__":
    main()
