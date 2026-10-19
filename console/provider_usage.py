"""Read-only, bounded OpenRouter key allowance lookup for administrators.

This cache never participates in chat admission and never contains account balance.
Only whitelisted numeric fields leave the server; provider bodies/errors do not.
"""
import asyncio
import hashlib
import json
import math
import time
from datetime import datetime, timezone

import httpx

from integration.provider_key import KeyUnavailable, ProviderKey


KEY_URL = "https://openrouter.ai/api/v1/key"
MIN_INTERVAL = 60
STALE_AFTER = 300
MAX_BODY = 65536


def amounts(body):
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise ValueError("Invalid key usage response")
    result = {}
    for source, target in (("usage", "usageUsd"), ("limit", "limitUsd"), ("limit_remaining", "remainingUsd")):
        value = data.get(source)
        if value is not None and (type(value) not in {int, float} or not math.isfinite(value)
                                  or source != "limit_remaining" and value < 0):
            raise ValueError("Invalid key usage amount")
        result[target] = value
    unlimited = "limit" in data and data["limit"] is None
    result["limitState"] = "unlimited" if unlimited else "limited" if result["limitUsd"] is not None else "unknown"
    result["limitReset"] = data.get("limit_reset") if data.get("limit_reset") in {None, "daily", "weekly", "monthly"} else "unknown"
    # Never derive remaining credit from lifetime usage: the limit may reset.
    complete = result["usageUsd"] is not None and (unlimited and "limit_remaining" in data and data["limit_remaining"] is None
               or result["limitUsd"] is not None and result["remainingUsd"] is not None)
    result["state"] = ("unlimited" if unlimited else "verified") if complete else "partial"
    return result


class ProviderUsage:
    def __init__(self, *, http=None, source=ProviderKey.from_environment, clock=time.monotonic, wall=time.time):
        self.http = http or httpx.AsyncClient(timeout=4, trust_env=False, follow_redirects=False)
        self.source, self.clock, self.wall = source, clock, wall
        self.cache = {}
        self.locks = {kind: asyncio.Lock() for kind in ("chat", "profiler")}

    async def close(self):
        await self.http.aclose()

    async def snapshot(self, kind):
        return await asyncio.to_thread(lambda: self.source(kind).snapshot())

    @staticmethod
    def identity(snapshot):
        # External file sources have no revision. This private digest is never returned.
        return snapshot.scope, snapshot.revision, hashlib.sha256(snapshot.value.encode()).digest()

    def empty(self, state, error=None):
        return {"state": state, "usageUsd": None, "limitUsd": None, "remainingUsd": None,
                "limitState": "unknown", "limitReset": None, "checkedAt": None, "attemptedAt": None,
                "ageSeconds": None, "staleAfterSeconds": STALE_AFTER, "retryAfterSeconds": 0,
                "errorCode": error}

    def stamp(self):
        return datetime.fromtimestamp(self.wall(), timezone.utc).isoformat()

    async def fetch(self, snapshot):
        async with asyncio.timeout(5):
            async with self.http.stream("GET", KEY_URL, headers={"Authorization": "Bearer " + snapshot.value},
                                        follow_redirects=False) as response:
                if response.status_code != 200:
                    return None, {401: "key_rejected", 403: "key_rejected", 429: "rate_limited"}.get(response.status_code, "provider_unavailable")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        return None, "invalid_response"
                return amounts(json.loads(body)), None

    async def get(self, kind):
        async with self.locks[kind]:
            try:
                snapshot = await self.snapshot(kind)
            except KeyUnavailable as exc:
                self.cache.pop(kind, None)
                return self.empty("unconfigured" if exc.code == "PROVIDER_KEY_MISSING" else "unavailable", "key_unavailable")
            identity = self.identity(snapshot)
            entry = self.cache.get(kind)
            if entry is None or entry["identity"] != identity:
                entry = {"identity": identity, "attempt": -math.inf, "data": None, "error": None}
                self.cache[kind] = entry
            if self.clock() - entry["attempt"] >= MIN_INTERVAL:
                entry.update(attempt=self.clock(), attemptedAt=self.stamp())
                try:
                    values, error = await self.fetch(snapshot)
                except (httpx.HTTPError, TimeoutError):
                    values, error = None, "provider_unavailable"
                except (ValueError, TypeError, OverflowError):
                    values, error = None, "invalid_response"
                # A key may be replaced while the network request is in flight.
                try:
                    unchanged = self.identity(await self.snapshot(kind)) == identity
                except KeyUnavailable:
                    unchanged = False
                if not unchanged:
                    self.cache.pop(kind, None)
                    return self.empty("unavailable", "key_changed")
                entry["error"] = error
                if values is not None:
                    entry.update(data=values, checkedAt=self.stamp(), checked=self.clock())
            result = self.empty("failed", entry["error"])
            if entry["data"] is not None:
                age = max(0, self.clock() - entry["checked"])
                result.update(entry["data"], checkedAt=entry["checkedAt"], ageSeconds=age)
                if age >= STALE_AFTER:
                    result["state"] = "stale"
            if entry["error"]:
                result["state"] = "failed"
            result.update(attemptedAt=entry.get("attemptedAt"),
                          retryAfterSeconds=math.ceil(max(0, MIN_INTERVAL - (self.clock() - entry["attempt"]))))
            return result

    async def status(self):
        values = await asyncio.gather(*(self.get(kind) for kind in self.locks))
        return dict(zip(self.locks, values))
