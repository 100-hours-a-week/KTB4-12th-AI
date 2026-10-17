"""Synchronous cost-only client for the same existing Chat cumulative account."""

import json
import logging
import math
import uuid
from urllib.parse import urlsplit

import httpx

ACCOUNT = "gift-chat-harness-2026-10-06"
PRICE_CAP = {"prompt": 0.11, "completion": 0.55, "request": 0}


class SharedBudget:
    def __init__(self, url, token, *, transport=None):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or not token
        ):
            raise ValueError(
                "An authenticated existing-ledger loopback gateway is required"
            )
        self.http = httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + token},
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def call(self, method, **args):
        response = self.http.post(
            "/rpc", json={"method": method, "args": {"account": ACCOUNT, **args}}
        )
        response.raise_for_status()
        result = response.json()["result"]
        if method == "budget_summary":
            if not isinstance(result, dict) or result.get("limitUsd") != 5:
                raise ValueError("Existing cumulative five-dollar ledger not verified")
        elif result is not True:
            raise ValueError("Budget operation not acknowledged")
        return result

    def reserve(self, body):
        body = dict(body)
        body["max_tokens"] = min(body.get("max_tokens", 1200), 1200)
        body["provider"] = {
            **body.get("provider", {}),
            "max_price": PRICE_CAP,
            "allow_fallbacks": False,
            "require_parameters": True,
        }
        size = len(json.dumps(body, ensure_ascii=False).encode())
        amount = max(
            0.01,
            math.ceil(
                (
                    (size + 4096) * PRICE_CAP["prompt"] / 1e6
                    + body["max_tokens"] * PRICE_CAP["completion"] / 1e6
                )
                * 2
                * 100
            )
            / 100,
        )
        ident = str(uuid.uuid4())
        self.call("budget_reserve", ident=ident, amount=amount, model=body["model"])
        return body, ident

    def finish(self, ident, cost):
        value = (
            cost
            if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
            else None
        )
        try:
            self.call(
                "budget_finish",
                ident=ident,
                cost=value,
                status="received" if value is not None else "unknown",
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            logging.getLogger(__name__).warning(
                "Shared settlement unconfirmed; durable central reservation retained"
            )

    def close(self):
        self.http.close()
