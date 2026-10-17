"""Container-local cost-only relay to an existing externally guarded ledger gateway."""

import hmac
import json
import math
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx

ACCOUNT = "gift-chat-harness-2026-10-06"


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_summary(value, attempts, reserved):
    if (
        type(attempts) is not int
        or not 1 <= attempts <= 100
        or not finite_number(reserved)
        or not 0 < reserved <= 1
    ):
        raise ValueError("Unsupported approved fence")
    required = {
        "limitUsd",
        "halted",
        "remainingUsd",
        "validationMaxAttempts",
        "validationMaxReservedUsd",
        "validationAttempts",
        "validationReservedUsd",
    }
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or type(value["limitUsd"]) not in (int, float)
        or value["limitUsd"] != 5
        or value["halted"] is not None
    ):
        raise ValueError("Existing five-dollar account not ready")
    if (
        type(value["validationMaxAttempts"]) is not int
        or value["validationMaxAttempts"] != attempts
        or not finite_number(value["validationMaxReservedUsd"])
        or value["validationMaxReservedUsd"] != reserved
    ):
        raise ValueError("Existing approved reservation fence missing/different")
    if (
        not finite_number(value["remainingUsd"])
        or value["remainingUsd"] <= 0
        or type(value["validationAttempts"]) is not int
        or not 0 <= value["validationAttempts"] < attempts
        or not finite_number(value["validationReservedUsd"])
        or not 0 <= value["validationReservedUsd"] < reserved
    ):
        raise ValueError("Existing allowance exhausted")
    return value


class BudgetRelay:
    def __init__(
        self, url, token, attempts, reserved, *, synthetic=False, transport=None
    ):
        p = urlsplit(url)
        allowed = {"host.docker.internal"} | (
            {"budget-fixture", "127.0.0.1"} if synthetic else set()
        )
        if (
            p.scheme not in ("http", "https")
            or p.hostname not in allowed
            or p.username
            or p.password
            or p.query
            or p.fragment
            or p.path not in ("", "/")
            or not p.port
            or not token
        ):
            raise ValueError("Explicit existing guarded budget origin required")
        self.http = httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + token},
            timeout=8,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self.token = token
        self.attempts = attempts
        self.reserved = reserved
        self.server = None
        self.thread = None

    @classmethod
    def from_environment(cls, token, *, synthetic=False):
        return cls(
            os.environ.get("BUDGET_GUARDED_ORIGIN", ""),
            token,
            int(os.environ["BUDGET_EXPECTED_MAX_ATTEMPTS"]),
            float(os.environ["BUDGET_EXPECTED_MAX_RESERVED_USD"]),
            synthetic=synthetic,
        )

    def forward(self, data):
        if (
            not isinstance(data, dict)
            or set(data) != {"method", "args"}
            or not isinstance(data["args"], dict)
        ):
            raise ValueError("Malformed budget RPC")
        method = data["method"]
        args = data["args"]
        allowed = {
            "budget_summary": {"account"},
            "budget_reserve": {"account", "ident", "amount", "model"},
            "budget_finish": {"account", "ident", "cost", "status"},
        }
        if (
            method not in allowed
            or args.get("account") != ACCOUNT
            or set(args) != allowed[method]
        ):
            raise ValueError("Only existing-account cost methods are accepted")
        if method == "budget_reserve":
            if (
                args["model"] != "openai/gpt-6-luna"
                or type(args["amount"]) not in (float, int)
                or not 0 < args["amount"] <= 0.10
            ):
                raise ValueError("Unapproved reservation")
            self.summary()
        if method == "budget_finish":
            cost = args["cost"]
            if (
                args["status"] not in ("received", "unknown")
                or (cost is None) != (args["status"] == "unknown")
                or (cost is not None and (not finite_number(cost) or cost < 0))
            ):
                raise ValueError("Invalid settlement")
        if method != "budget_summary" and (
            not isinstance(args["ident"], str) or not 1 <= len(args["ident"]) <= 128
        ):
            raise ValueError("Invalid reservation identity")
        response = self.http.post("/rpc", json=data)
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Budget acknowledgment missing")
        result = response.json()["result"]
        if method == "budget_summary":
            validate_summary(result, self.attempts, self.reserved)
        elif result is not True:
            raise ValueError("Budget acknowledgment missing")
        return {"result": result}

    def summary(self):
        return self.forward({"method": "budget_summary", "args": {"account": ACCOUNT}})[
            "result"
        ]

    def start(self):
        self.summary()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    if (
                        self.path != "/rpc"
                        or self.headers.get("Origin")
                        or not hmac.compare_digest(
                            self.headers.get("Authorization", ""),
                            "Bearer " + relay.token,
                        )
                    ):
                        raise ValueError()
                    self.connection.settimeout(8)
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 8192:
                        raise ValueError()
                    body = relay.forward(json.loads(self.rfile.read(size)))
                    status = 200
                except (ValueError, TypeError, KeyError, OSError, httpx.HTTPError):
                    body = {
                        "message": "Existing guarded budget unavailable; no paid send permitted"
                    }
                    status = 503
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=5)
        self.http.close()
