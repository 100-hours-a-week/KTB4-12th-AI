"""Authenticated local diagnostics and write-only settings; no arbitrary proxy."""

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse, RedirectResponse
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field
from integration.profile_contract import ProfileInput
from integration.profiler_health import model_state
from integration.database_identity import VERSION as DATABASE_VERSION
from console.products import ProductDisplay
from console.admin_auth import AdminAuth, private_json
from console.provider_usage import ProviderUsage
from integration.provider_key import ProviderKey, KeyUnavailable, KeyConflict, environment
from integration.service_token import ServiceToken, shared_token_configured

IMPORTED_SERVER_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

ROOT = Path(__file__).parent
STAMP = lambda: datetime.now(timezone.utc).isoformat()
HTTP_TRACE = ContextVar("console_http_trace", default=None)
SAFE_HEADERS = {"content-type", "retry-after", "cache-control"}


def http_masked(value, key="", depth=0):
    """HTTP history is permanently masked and bounded, independent of raw SSE viewing."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if key.lower() in {
        "message",
        "text",
        "delta",
        "reason",
        "giftpreference",
        "progressmessage",
        "query",
        "querytext",
        "query_text",
        "recipientcontext",
        "input",
        "ctx",
        "msg",
        "reviews",
        "dislikedcategories",
        "preferredtags",
        "dislikedtags",
        "evidence",
        "cookie",
        "set-cookie",
        "prompt",
        "systemprompt",
        "providerresponse",
        "provider_response",
        "providerbody",
        "callbackurl",
        "callback_url",
        "rawresponse",
    }:
        return "[원문 숨김]"
    if key.lower() == "detail" and isinstance(value, str):
        return "[오류 상세 숨김]"
    if depth >= 8:
        return "[깊이 제한]"
    value = masked(value, key)
    if isinstance(value, dict):
        return {k: http_masked(v, k, depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, list):
        return [http_masked(v, "", depth + 1) for v in value[:30]]
    return value[:1500] if isinstance(value, str) else value


def redact_known(value, secret):
    if not secret:
        return value
    if isinstance(value, dict):
        return {k.replace(secret, "[redacted]"): redact_known(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_known(v, secret) for v in value]
    return value.replace(secret, "[redacted]") if isinstance(value, str) else value


def response_secret(response):
    try:
        value = response.request.headers.get("Authorization", "")
        return value[7:] if value.startswith("Bearer ") else ""
    except (RuntimeError, AttributeError):
        return ""


def redact_event_frame(frame, secret):
    if not secret:
        return frame
    lines = frame.split("\n")
    data = [line[5:].lstrip(" ") for line in lines if line.startswith("data:")]
    if not data:
        return redact_known(frame, secret)
    try:
        value = json.loads("\n".join(data))
    except ValueError:
        # Unexpected raw data must not bypass JSON escaping/redaction.
        value = {"error": "UNRECOGNIZED_UPSTREAM_EVENT"}
    clean = redact_known(value, secret)
    rendered = []
    wrote = False
    for line in lines:
        if line.startswith("data:"):
            if not wrote:
                rendered.append("data: " + json.dumps(clean, ensure_ascii=False))
                wrote = True
        else:
            rendered.append(redact_known(line, secret))
    return "\n".join(rendered)


def safe_headers(headers, secret=""):
    return {
        k.lower(): masked(redact_known(v, secret))[:200]
        for k, v in headers.items()
        if k.lower() in SAFE_HEADERS
    }


def trace_exchange(
    service, method, path, body, started, response=None, payload=None, error=None
):
    return {
        "service": service,
        "method": method,
        "path": path,
        "requestBody": http_masked(body),
        "responseBody": http_masked(redact_known(payload, response_secret(response))),
        "status": response.status_code if response is not None else None,
        "headers": safe_headers(response.headers, response_secret(response)) if response is not None else {},
        "durationMs": (time.perf_counter() - started) * 1000,
        "error": error,
    }


def public_health(service, data):
    """Only operational fields cross the health boundary; no arbitrary payloads."""
    if not isinstance(data, dict):
        return {}
    fields = {
        "chat": (
            "ready",
            "database",
            "mode",
            "model",
            "agent",
            "reasoningLabel",
            "budgetAccount",
            "budgetMode",
        ),
        "search": ("status", "products", "snapshotId", "catalogLoadError"),
        "profiling": ("status",),
    }
    safe = {
        k: data[k]
        for k in fields.get(service, ())
        if k in data and (data[k] is None or type(data[k]) in (str, int, float, bool))
    }
    nested = {
        "chat": {
            "sharedBudget": (
                "limitUsd",
                "committedUsd",
                "remainingUsd",
                "attempts",
                "halted",
                "validationAttempts",
                "validationReservedUsd",
                "validationMaxAttempts",
                "validationMaxReservedUsd",
            )
        },
        "profiling": {
            "catalog": ("active", "version", "products"),
            "store": ("backend", "connected", "migration", "undelivered"),
            "supervisor": (
                "slots",
                "running",
                "queued",
                "queue_max",
                "submitted",
                "completed",
                "rejected",
                "failed",
            ),
            "tagger": ("enabled", "required", "model"),
            "llm": ("enabled", "model", "requested", "state", "last", "consecutive_failures"),
            "integration": ("modelRequired", "budgetGuard", "catalogVersionBound"),
            "embedding": ("enabled",),
        },
    }
    for key, names in nested.get(service, {}).items():
        value = data.get(key)
        if isinstance(value, dict):
            safe[key] = {
                k: value[k]
                for k in names
                if k in value
                and (value[k] is None or type(value[k]) in (str, int, float, bool))
            }
    # catalogLoadError can contain arbitrary provider/database exception text.
    if safe.get("catalogLoadError") is not None:
        safe["catalogLoadError"] = "카탈로그 로드 오류 · 원문 숨김"
    return safe


def target_url(value):
    url = urlsplit(value)
    private_mesh = (
        os.getenv("CONSOLE_SERVICE_HOST") == "mesh"
        and url.hostname == "mesh"
        and url.port in {7860, 8000, 8001, 8002}
    )
    if (
        url.scheme != "http"
        or (url.hostname not in {"127.0.0.1", "localhost", "::1"} and not private_mesh)
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise ValueError("Targets must be fixed loopback or explicitly enabled private mesh HTTP origins")
    return value.rstrip("/")


def masked(value, key=""):
    if any(
        word in key.lower()
        for word in [
            "token",
            "secret",
            "password",
            "authorization",
            "apikey",
            "api_key",
            "credential",
            "csrf",
            "cookie",
            "session",
            "hash",
        ]
    ):
        return "[redacted]"
    if isinstance(value, dict):
        return {k: masked(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(v) for v in value]
    return (
        re.sub(
            r"Bearer\s+[^\s\"']+|\b(?:sk-|hf_)[A-Za-z0-9_-]{12,}", "[redacted]", value
        )
        if isinstance(value, str)
        else value
    )


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatInput(Input):
    request_id: uuid.UUID
    conversation_id: uuid.UUID
    turn_id: uuid.UUID
    message: str = Field(min_length=1, max_length=3000)
    senderUserId: int = Field(default=7, ge=1)
    recipientUserId: int = Field(default=42, ge=1)
    recipientContext: dict | None = None
    preparedContext: dict | None = None


class Backend:
    def __init__(self, config=None, *, admission_store=None):
        self.config = config or {}
        self.demo = not config
        self.targets = {
            k: target_url(v) for k, v in self.config.get("targets", {}).items()
        }
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(30, read=180), trust_env=False, follow_redirects=False
        )
        self.turns = {}
        self.profiles = {}
        self.admissions = {}
        self.pending_turns = set()
        self.admission_store = admission_store if not self.demo else None
        self.target_scope = hashlib.sha256(json.dumps({
            "targets": self.targets, "liveModel": bool(self.config.get("liveModel")),
            "budgetMode": self.config.get("budgetMode", "provider-key-limits"),
            "database": self.config.get("database"),
        }, sort_keys=True).encode()).hexdigest()
        self.profile_inputs = {}
        self.lock = asyncio.Lock()
        self.profile_lock = asyncio.Lock()
        self.jobs = set()
        self.salt = secrets.token_bytes(16)
        self.started_at = STAMP()
        self.started_monotonic = time.monotonic()

    def anon(self, value):
        return hashlib.sha256(self.salt + str(value).encode()).hexdigest()[:10]

    def saved_admission(self, rid, session_key):
        if self.admission_store is not None:
            return self.admission_store.chat_admission(session_key, self.target_scope, request_id=rid)
        row = self.admissions.get(rid)
        if row and row.get("owner") != session_key:
            raise HTTPException(409, "CONSOLE_CHAT_OWNERSHIP_MISMATCH")
        return row

    def save_admission(self, rid, row, session_key):
        if self.admission_store is not None:
            self.admission_store.save_chat_admission(session_key, self.target_scope, row)
        else:
            row["owner"] = session_key
            self.admissions[rid] = row

    def rejected_admission(self, rid, row, old, session_key):
        if self.admission_store is not None:
            row["readable"] = bool(old and old.get("readable"))
            self.save_admission(rid, row, session_key)
        elif old:
            self.admissions[rid] = old
        else:
            self.admissions.pop(rid, None)

    def auth(self, service):
        if shared_token_configured() and service in {"chat", "profiling"}:
            try:
                source = ServiceToken.from_environment("chat" if service == "chat" else "profiler")
                return {"Authorization": "Bearer " + source.snapshot().value}
            except KeyUnavailable:
                raise HTTPException(503, "SERVICE_TOKEN_UNAVAILABLE") from None
        token = self.config.get("tokens", {}).get(service, "")
        return {"Authorization": "Bearer " + token} if token else {}

    def chat_base(self):
        return (
            "/v1/chat/conversations/"
            if self.config.get("liveModel")
            else "/v1/chat/test/conversations/"
        )

    def budget_policy_matches(self, ready, service="chat"):
        mode = self.config.get("budgetMode", "provider-key-limits")
        if mode not in {"guarded", "provider-key-limits"}:
            return False
        if service == "profiling":
            integration = ready.get("integration")
            expected = "central-account" if mode == "guarded" else mode
            return isinstance(integration, dict) and integration.get("budgetGuard") == expected
        # Older guarded servers omitted this field. Provider limits require an
        # explicit matching runtime policy; configuration alone is not evidence.
        return ready.get("budgetMode") == mode if "budgetMode" in ready or mode == "provider-key-limits" else True

    async def check_live_budget(self):
        mode = self.config.get("budgetMode", "provider-key-limits")
        if not self.config.get("liveModel") or mode not in {"guarded", "provider-key-limits"}:
            raise HTTPException(409, "Live mode and an explicit supported cost policy are required")
        status, ready = await self.request("chat", "GET", "/readyz")
        ready = ready if isinstance(ready, dict) else {}
        expected_model = self.config.get("expectedChatModel", self.config.get("expectedModel"))
        if (
            status != 200
            or ready.get("ready") is not True
            or ready.get("mode") != "live"
            or ready.get("database") != "postgresql"
            or not isinstance(ready.get("model"), str)
            or not ready["model"].strip()
            or (expected_model and ready.get("model") != expected_model)
            or not self.budget_policy_matches(ready)
        ):
            raise HTTPException(409, "Actual live runtime not verified")
        # PR20 explicitly delegates cost limits to the provider in this mode.
        # Missing ledger data alone must never select that policy.
        if mode == "provider-key-limits":
            return ready
        budget = ready.get("sharedBudget", {})
        if (
            ready.get("budgetAccount") != "gift-chat-harness-2026-10-06"
            or not isinstance(budget, dict)
            or type(budget.get("limitUsd")) not in (int, float)
            or budget.get("limitUsd") != 5
            or budget.get("halted") is not None
            or type(budget.get("remainingUsd")) not in (int, float)
            or not math.isfinite(budget["remainingUsd"])
            or budget["remainingUsd"] <= 0
        ):
            raise HTTPException(
                409, "Actual live mode and existing cumulative ledger not verified"
            )
        return ready

    async def request(self, service, method, path, body=None):
        started = time.perf_counter()
        trace = HTTP_TRACE.get()
        try:
            response = await self.http.request(
                method,
                self.targets[service] + path,
                json=body,
                headers=await asyncio.to_thread(self.auth, service),
                **({"timeout": 5} if method == "GET" and path in {"/readyz", "/health", "/internal/credentials"} else {}),
            )
        except httpx.HTTPError as exc:
            if trace is not None:
                trace.append(
                    trace_exchange(
                        service, method, path, body, started, error=type(exc).__name__
                    )
                )
            raise
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": {"code": "NON_JSON_RESPONSE"}}
        if trace is not None:
            trace.append(
                trace_exchange(service, method, path, body, started, response, payload)
            )
        return response.status_code, masked(redact_known(payload, response_secret(response)))

    def start_job(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)

    async def chat(self, body, session_key=None):
        data = body.model_dump(mode="json")
        rid = data["request_id"]
        fingerprint = hashlib.sha256(
            json.dumps(data, sort_keys=True).encode()
        ).hexdigest()
        async with self.lock:
            if self.admission_store is None and len(self.admissions) >= 256 and rid not in self.admissions:
                raise HTTPException(
                    429, "Console history limit reached; restart to clear"
                )
            old = self.saved_admission(rid, session_key)
            if old and old["fingerprint"] != fingerprint:
                raise HTTPException(
                    409, "Same request ID requires identical UUIDs and body"
                )
            if not self.demo:
                if old and old.get("readable"):
                    # An accepted turn is a read even after new-work readiness or
                    # budget closes. Only a confirmed absence may retry admission.
                    status, result = await self.request(
                        "chat", "GET", self.chat_base() + data["conversation_id"]
                        + "/turns/" + data["turn_id"],
                    )
                    state = result.get("data") if isinstance(result, dict) else None
                    if status == 200:
                        if not isinstance(state, dict) or state.get("status") not in {
                            "PROCESSING", "COMPLETED", "CANCELLED", "FAILED",
                        }:
                            raise HTTPException(502, "Upstream Chat state is invalid")
                        receipt = {
                            "httpStatus": 202 if state["status"] == "PROCESSING" else 200,
                            "body": result, "requestId": rid,
                            "conversationId": data["conversation_id"],
                            "turnId": data["turn_id"], "receivedAt": STAMP(),
                        }
                        old["receipt"] = receipt
                        self.save_admission(rid, old, session_key)
                        return receipt
                    if status != 404:
                        raise HTTPException(
                            status if 400 <= status <= 599 else 502,
                            "Upstream Chat state query failed",
                        )
                if self.config.get("liveModel"):
                    ready = await self.check_live_budget()
                    if old:
                        data = old["body"]
                    elif "backend" in self.targets and data["recipientContext"] is None:
                        context_status, context = await self.request(
                            "backend",
                            "GET",
                            f"/fixture/recipients/{data['recipientUserId']}/chat-context",
                        )
                        if (
                            context_status != 200
                            or context.get("synthetic") is not True
                        ):
                            raise HTTPException(
                                409, "Synthetic BE contract snapshot unavailable"
                            )
                        data.update(
                            {
                                k: context.get(k)
                                for k in ("recipientContext", "preparedContext")
                            }
                        )
                else:
                    status, ready = await self.request("chat", "GET", "/readyz")
                    if status != 200 or ready.get("mode") != "mock":
                        raise HTTPException(409, "Paid or unknown chat runtime is blocked by this console")
                # Freeze the complete request before any mutating upstream send.
                # A lost acknowledgement must never refetch a newer profile.
                row = {
                    "fingerprint": fingerprint,
                    "receipt": old.get("receipt") if old else None,
                    "body": data,
                    "readable": bool(old and old.get("readable")),
                    "conversationOwned": bool(old and old.get("conversationOwned")),
                }
                self.save_admission(rid, row, session_key)
                known_conversation = (self.admission_store.owns_chat_conversation(
                    session_key, self.target_scope, data["conversation_id"])
                    if self.admission_store is not None else True)
                base = self.chat_base() + data["conversation_id"]
                trace = HTTP_TRACE.get()
                trace_start = len(trace) if trace is not None else 0
                try:
                    status, result = await self.request(
                        "chat", "PUT", base,
                        {"senderUserId": data["senderUserId"],
                         "recipientUserId": data["recipientUserId"]},
                    )
                except httpx.HTTPError:
                    if not known_conversation:
                        # No turn was sent. An explicit new conversation is safe;
                        # retrying an arbitrary existing UUID cannot prove ownership.
                        raise HTTPException(409, "CONSOLE_CONVERSATION_UNCONFIRMED") from None
                    raise
                finally:
                    # A 200 may contain a previous login's turn metadata. Only
                    # status/timing may leave this boundary until ownership is known.
                    if not known_conversation and trace is not None:
                        for exchange in trace[trace_start:]:
                            exchange["responseBody"] = None
                if status not in {200, 201}:
                    self.rejected_admission(rid, row, old, session_key)
                    return {"httpStatus": status, "body": result}
                if status == 200 and not known_conversation:
                    # A supplied UUID must not claim a conversation from another
                    # login, including one whose expired admission was purged.
                    raise HTTPException(409, "CONSOLE_CONVERSATION_NOT_OWNED")
                row["conversationOwned"] = True
                # Persist before the turn send: process death or a lost reply
                # must leave a read-only recovery path, never an automatic send.
                if not old or not old.get("readable"):
                    self.pending_turns.add(data["turn_id"])
                try:
                    if self.admission_store is not None:
                        row["readable"] = True
                    self.save_admission(rid, row, session_key)
                    status, result = await self.request(
                        "chat",
                        "PUT",
                        base + "/turns/" + data["turn_id"],
                        {
                            "message": data["message"],
                            **{
                                k: data[k]
                                for k in ("recipientContext", "preparedContext")
                                if data[k] is not None
                            },
                        },
                    )
                except (httpx.ReadError, httpx.ReadTimeout, httpx.WriteError,
                        httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.DecodingError):
                    # The turn may have been accepted before its acknowledgement
                    # was lost. Keep its frozen body and allow recovery.
                    row["readable"] = True
                    self.save_admission(rid, row, session_key)
                    raise
                except httpx.HTTPError:
                    self.rejected_admission(rid, row, old, session_key)
                    raise
                finally:
                    self.pending_turns.discard(data["turn_id"])
                if status not in {200, 202}:
                    if status == 408 or status >= 500:
                        row["readable"] = True
                        self.save_admission(rid, row, session_key)
                    else:
                        self.rejected_admission(rid, row, old, session_key)
                    return {"httpStatus": status, "body": result}
            elif old:
                stored = self.turns[data["turn_id"]]
                status = 202 if stored["status"] == "PROCESSING" else 200
                result = {"data": {"status": stored["status"]}}
            else:
                status, result = 202, {"data": {"status": "PROCESSING"}}
                turn = {
                    "status": "PROCESSING",
                    "events": [],
                    "created": STAMP(),
                    "message": data["message"],
                    "conversation": data["conversation_id"],
                }
                self.turns[data["turn_id"]] = turn
                self.start_job(self.produce(data["turn_id"]))
            receipt = {
                "httpStatus": status,
                "body": result,
                "requestId": rid,
                "conversationId": data["conversation_id"],
                "turnId": data["turn_id"],
                "receivedAt": STAMP(),
            }
            self.save_admission(rid, {
                "fingerprint": fingerprint,
                "receipt": receipt,
                "body": data,
                "readable": True,
                "conversationOwned": True,
            }, session_key)
            return receipt

    async def produce(self, tid):
        turn = self.turns[tid]
        scenario = turn["message"]

        def emit(kind, payload):
            turn["events"].append(
                {"id": len(turn["events"]) + 1, "event": kind, "data": payload}
            )

        emit("progress", {"message": "합성 처리 중"})
        count = 30 if "slow" in scenario else 4
        for i in range(count):
            await asyncio.sleep(0.15)
            if turn["status"] != "PROCESSING":
                return
            emit(
                "answer.delta", {"delta": "합성 스트리밍 응답 " if i == 0 else "블록 "}
            )
            if "failed" in scenario and i == 1:
                turn["status"] = "FAILED"
                emit(
                    "turn.finished",
                    {"status": "FAILED", "error": {"code": "SYNTHETIC_FAILURE"}},
                )
                return
        if "cards" in scenario:
            emit(
                "cards",
                {"products": [{"productId": 101, "reason": "합성 테스트 추천 이유"}]},
            )
        turn["status"] = "COMPLETED"
        emit("turn.finished", {"status": "COMPLETED"})

    def admission(self, tid, session_key=None):
        if tid in self.pending_turns:
            raise HTTPException(404, "Chat acceptance is still being confirmed")
        row = self.admission_store.chat_admission(session_key, self.target_scope, turn_id=tid) if self.admission_store is not None else next(
            (a for a in self.admissions.values()
             if a["body"]["turn_id"] == tid and a.get("owner") == session_key), None
        )
        if not row or not row.get("readable"):
            raise HTTPException(
                404, "Only turns created in this console session are readable"
            )
        return row

    async def state(self, tid, session_key=None):
        row = self.admission(tid, session_key)
        if self.demo:
            turn = self.turns[tid]
            return {
                "data": {"status": turn["status"], "lastEventId": len(turn["events"])}
            }
        status, result = await self.request(
            "chat",
            "GET",
            self.chat_base() + row["body"]["conversation_id"] + "/turns/" + tid,
        )
        if status != 200:
            raise HTTPException(status, "Upstream Chat state query failed")
        return result

    async def cancel(self, tid, session_key=None):
        row = self.admission(tid, session_key)
        if self.demo:
            turn = self.turns[tid]
            if turn["status"] == "PROCESSING":
                turn["status"] = "CANCELLED"
                turn["events"].append(
                    {
                        "id": len(turn["events"]) + 1,
                        "event": "turn.finished",
                        "data": {"status": "CANCELLED"},
                    }
                )
            return {"httpStatus": 200, "body": {"data": {"status": turn["status"]}}}
        status, payload = await self.request(
            "chat",
            "PUT",
            self.chat_base()
            + row["body"]["conversation_id"]
            + "/turns/"
            + tid
            + "/cancellation",
        )
        return {"httpStatus": status, "body": payload}

    async def events(self, tid, after, session_key=None):
        row = self.admission(tid, session_key)
        if self.demo:
            yield 'event: console.http\ndata: {"mode":"synthetic","exchanges":[]}\n\n'
            while True:
                turn = self.turns[tid]
                for item in turn["events"]:
                    if item["id"] > after:
                        after = item["id"]
                        yield (
                            "id: "
                            + str(after)
                            + "\nevent: "
                            + item["event"]
                            + "\ndata: "
                            + json.dumps(item["data"], ensure_ascii=False)
                            + "\n\n"
                        )
                if turn["status"] != "PROCESSING":
                    return
                await asyncio.sleep(0.08)
        else:
            path = (
                self.chat_base()
                + row["body"]["conversation_id"]
                + "/turns/"
                + tid
                + "/events"
            )
            started = time.perf_counter()
            response = None
            try:
                async with self.http.stream(
                    "GET",
                    self.targets["chat"] + path,
                    params={"afterEventId": after},
                    headers=await asyncio.to_thread(self.auth, "chat"),
                ) as response:
                    payload = {"stream": "SSE body is recorded separately as events"}
                    if response.status_code != 200:
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk[: 16384 - len(raw)])
                            if len(raw) >= 16384:
                                break
                        try:
                            payload = json.loads(raw)
                        except (ValueError, UnicodeError):
                            payload = {"error": {"code": "NON_JSON_RESPONSE"}}
                    exchange = trace_exchange(
                        "chat",
                        "GET",
                        path,
                        None,
                        started,
                        response,
                        payload,
                    )
                    exchange["query"] = {"afterEventId": after}
                    yield (
                        "event: console.http\ndata: "
                        + json.dumps({"mode": "attempted", "exchanges": [exchange]})
                        + "\n\n"
                    )
                    if response.status_code != 200:
                        yield (
                            "event: console.error\ndata: "
                            + json.dumps({"httpStatus": response.status_code})
                            + "\n\n"
                        )
                        return
                    # Never mix relay diagnostics into an unfinished upstream frame.
                    # Forward complete frames only so a reconnect keeps its last
                    # durable event cursor; partial data is replayed by upstream.
                    pending = ""
                    async for chunk in response.aiter_text():
                        pending = (pending + chunk).replace("\r\n", "\n")
                        while "\n\n" in pending:
                            frame, pending = pending.split("\n\n", 1)
                            if len(frame) > 1048576:
                                raise httpx.ReadError(
                                    "Upstream SSE frame exceeds limit"
                                )
                            yield redact_event_frame(frame, response_secret(response)) + "\n\n"
                        if len(pending) > 1048576:
                            raise httpx.ReadError("Upstream SSE frame exceeds limit")
                    if pending.strip():
                        raise httpx.ReadError("Incomplete upstream SSE frame")
            except httpx.HTTPError as exc:
                exchange = trace_exchange(
                    "chat",
                    "GET",
                    path,
                    None,
                    started,
                    response=response,
                    error=type(exc).__name__,
                )
                exchange["query"] = {"afterEventId": after}
                yield (
                    "event: console.http\ndata: "
                    + json.dumps({"mode": "attempted", "exchanges": [exchange]})
                    + "\n\n"
                )
                yield 'event: console.error\ndata: {"code":"UPSTREAM_CONNECTION_FAILED"}\n\n'

    async def check_profile_ready(self):
        if not self.demo:
            if self.config.get("liveModel"):
                if self.config.get("budgetMode", "provider-key-limits") == "guarded":
                    await self.check_live_budget()
                ready_status, ready = await self.request("profiling", "GET", "/readyz")
                checks = ready.get("checks") if isinstance(ready, dict) else None
                if (ready_status != 200 or not isinstance(ready, dict) or ready.get("ready") is not True
                        or not isinstance(checks, dict) or not all(checks.get(key) is True for key in
                            ("catalog", "db", "migration", "integration_required", "required_embedding"))):
                    raise HTTPException(409, "Required Profiler runtime is not ready")
                health_status, health = await self.request(
                    "profiling", "GET", "/health"
                )
                if not isinstance(health, dict) or any(
                    not isinstance(health.get(field, {}), dict)
                    for field in ("llm", "integration", "tagger", "embedding", "store", "catalog")
                ):
                    raise HTTPException(409, "Required actual tagging/embedding runtime not verified")
                if not self.budget_policy_matches(health, "profiling"):
                    raise HTTPException(409, "Actual Profiler cost policy not verified")
                tagger = model_state(health)
                expected_model = self.config.get("expectedProfilerModel", self.config.get("expectedModel"))
                if (
                    health_status != 200
                    or tagger.get("enabled") is not True
                    or tagger.get("required") is not True
                    or health.get("embedding", {}).get("enabled") is not True
                    or health.get("store", {}).get("connected") is not True
                    or health.get("catalog", {}).get("active") is not True
                    or not isinstance(tagger.get("model"), str)
                    or not tagger["model"].strip()
                    or (expected_model and tagger.get("model") != expected_model)
                ):
                    raise HTTPException(
                        409, "Required actual tagging/embedding runtime not verified"
                    )
            if (
                not (
                    self.config.get("localFixtureProfilerFree")
                    or self.config.get("liveModel")
                )
                or not self.config.get("database")
                or (not self.config.get("liveModel") and not self.config.get("launcherPid"))
            ):
                raise HTTPException(
                    409, "Profiler model policy is unknown; request sending is disabled"
                )
            if not self.config.get("liveModel"):
                try:
                    os.kill(self.config["launcherPid"], 0)
                except (OSError, TypeError):
                    raise HTTPException(409, "The controlled fixture launcher is no longer running")
            try:
                with psycopg.connect(self.config["database"], connect_timeout=2) as db:
                    db.execute("SET TRANSACTION READ ONLY")
                    db.execute("SET LOCAL statement_timeout='2000ms'")
                    if self.config.get("liveModel"):
                        expected = psycopg.conninfo.conninfo_to_dict(self.config["database"]).get("dbname")
                        identity = db.execute("SELECT current_database(),database_name,version FROM ai_runtime.database_identity WHERE singleton").fetchone()
                        if not expected or identity != (expected, expected, DATABASE_VERSION):
                            raise HTTPException(409, "Prepared application database identity not verified")
                    elif db.execute("SELECT count(*) FROM ai_catalog.integration_marker").fetchone()[0] != 1:
                        raise HTTPException(409, "An isolated fixture database is required")
            except psycopg.Error:
                raise HTTPException(409, "Prepared application database not verified") from None

    async def profile(self, body):
        async with self.profile_lock:
            return await self.accept_profile(body)

    async def accept_profile(self, body):
        key = str(body.recipientUserId) + ":" + str(body.sourceVersion)
        fingerprint = hashlib.sha256(
            json.dumps(body.model_dump(), sort_keys=True).encode()
        ).hexdigest()
        if key in self.profile_inputs and self.profile_inputs[key] != fingerprint:
            raise HTTPException(409, "Same recipient/version requires identical input")
        if len(self.profile_inputs) >= 256 and key not in self.profile_inputs:
            raise HTTPException(429, "Console history limit reached; restart to clear")
        if not self.demo:
            await self.check_profile_ready()
            if self.config.get("liveModel") and "backend" in self.targets:
                bound_status, bound = await self.request(
                    "backend", "PUT",
                    f"/fixture/recipients/{body.recipientUserId}/profile-input",
                    body.model_dump(),
                )
                if bound_status != 200 or bound.get("inputBound") is not True or bound.get("synthetic") is not True:
                    return {"httpStatus": bound_status if bound_status != 200 else 409, "body": {"message": "Synthetic profile input binding failed"}, "key": key, "receivedAt": STAMP()}
            status, result = await self.request(
                "profiling",
                "POST",
                "/api/internal/v1/ai/profile/extract-and-pool",
                body.model_dump(),
            )
        else:
            status, result = (
                202,
                {
                    "data": {
                        "profileStatus": "PENDING",
                        "sourceVersion": body.sourceVersion,
                    }
                },
            )
            if key not in self.profiles:
                self.profiles[key] = {
                    "status": "RUNNING",
                    "created": STAMP(),
                    "updated": STAMP(),
                    "callbackAttempts": 0,
                    "recipient": body.recipientUserId,
                    "version": body.sourceVersion,
                }
                self.start_job(self.produce_profile(key, body.giftPreference))
        if status == 202:
            self.profile_inputs[key] = fingerprint
        return {"httpStatus": status, "body": result, "key": key, "receivedAt": STAMP()}

    async def produce_profile(self, key, preference):
        await asyncio.sleep(0.7)
        row = self.profiles[key]
        row.update(
            status="FAILED" if preference and "/fail" in preference else "RESULT_READY",
            updated=STAMP(),
        )
        if row["status"] != "FAILED":
            await asyncio.sleep(0.6)
            row.update(status="DELIVERED", updated=STAMP(), callbackAttempts=1)

    def database_snapshot(self):
        with psycopg.connect(
            self.config["database"], connect_timeout=2, row_factory=dict_row
        ) as db:
            db.execute("SET TRANSACTION READ ONLY")
            db.execute("SET LOCAL statement_timeout='2000ms'")
            chat = db.execute(
                "SELECT state->>'status' status,count(*) n FROM ai_chat.turns WHERE created>=now()-interval '24 hours' GROUP BY status"
            ).fetchall()
            active_chat = db.execute(
                "SELECT count(*) n FROM ai_chat.turns WHERE state->>'status'='PROCESSING'"
            ).fetchone()["n"]
            active_profile = db.execute(
                "SELECT count(*) n FROM ai_profile.profile_runs WHERE status='RUNNING'"
            ).fetchone()["n"]
            jobs = db.execute("""SELECT t.id,t.state->>'status' status,t.created,
                t.state->>'lastEventId' event_cursor,
                (SELECT kind FROM ai_chat.events WHERE turn=t.id ORDER BY seq DESC LIMIT 1) last_event,
                t.context->>'activeStage' recorded_stage,t.context->>'agentFailureCode' error_code,
                t.context->'elapsedMs' elapsed_ms,
                jsonb_path_query_array(t.state,'$.blocks[*] ? (@.type == "cards").products[*].productId') product_ids
                FROM ai_chat.turns t WHERE t.created>=now()-interval '24 hours' OR t.state->>'status'='PROCESSING'
                ORDER BY (t.state->>'status'='PROCESSING') DESC,t.created DESC LIMIT 25""").fetchall()
            profiles = db.execute("""SELECT id,status,created_at,updated_at,recipient_user_id,source_version,
                callback_attempts,error->>'code' error_code,tag_status,llm_cost_usd
                FROM ai_profile.profile_runs WHERE created_at>=now()-interval '24 hours' OR status='RUNNING'
                ORDER BY (status='RUNNING') DESC,updated_at DESC LIMIT 25""").fetchall()
            counts = db.execute(
                "SELECT status,count(*) n FROM ai_profile.profile_runs WHERE created_at>=now()-interval '24 hours' GROUP BY status"
            ).fetchall()
            budget = None
            if self.config.get("budgetMode", "provider-key-limits") == "guarded":
                budget = db.execute(
                    "SELECT count(*) n,sum(actual_usd) actual,sum(reserved_usd) FILTER(WHERE actual_usd IS NULL) reserved,count(*) FILTER(WHERE actual_usd IS NULL) unconfirmed_n FROM ai_chat.task_budget_attempts WHERE created>=now()-interval '24 hours'"
                ).fetchone()
        now = datetime.now(timezone.utc)

        def code(value):
            return (
                value
                if isinstance(value, str)
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", value)
                else None
            )

        rows = []
        for row in jobs:
            active = row["status"] == "PROCESSING"
            phase = {
                "progress": "진행 이벤트 수신",
                "answer.delta": "답변 스트리밍",
                "cards": "상품 카드 저장",
                "turn.finished": "최종 상태 저장",
            }.get(
                row["last_event"], "접수 후 이벤트 대기" if active else "최종 상태 저장"
            )
            duration = row["elapsed_ms"]
            rows.append(
                {
                    "id": self.anon(row["id"]),
                    "service": "Chat",
                    "status": row["status"],
                    "created": row["created"].isoformat(),
                    "stage": phase,
                    "stageSource": "stored last event",
                    "lastEvent": row["last_event"],
                    "eventCursor": int(row["event_cursor"] or 0),
                    "errorCode": code(row["error_code"]),
                    "failureStage": code(row["recorded_stage"]),
                    "elapsedSeconds": max(0, (now - row["created"]).total_seconds())
                    if active
                    else duration / 1000
                    if type(duration) in (int, float) and duration >= 0
                    else None,
                    "elapsedBasis": "접수 후 경과 · DB"
                    if active
                    else "저장 실행 시간"
                    if type(duration) in (int, float)
                    else "종료 시간 미계측",
                    "active": active,
                    "productIds": [
                        v
                        for v in row["product_ids"]
                        if type(v) is int and 0 < v <= 2**53 - 1
                    ],
                }
            )
        for row in profiles:
            active = row["status"] == "RUNNING"
            rows.append(
                {
                    "id": self.anon(row["id"]),
                    "service": "Profiler",
                    "status": row["status"],
                    "created": row["created_at"].isoformat(),
                    "updated": row["updated_at"].isoformat(),
                    "stage": {
                        "RUNNING": "분석 실행 · 세부 단계 미계측",
                        "RESULT_READY": "분석 완료 · 콜백 전달 대기",
                        "DELIVERED": "콜백 수락 응답 저장",
                        "SUPERSEDED": "구버전 콜백 폐기",
                        "FAILED": "실패 상태 저장",
                    }.get(row["status"], "단계 미수집"),
                    "stageSource": "stored run status",
                    "callbackAttempts": row["callback_attempts"],
                    "errorCode": code(row["error_code"]),
                    "tagStatus": row["tag_status"],
                    "elapsedSeconds": max(0, (now - row["created_at"]).total_seconds())
                    if active
                    else None,
                    "elapsedBasis": "접수 후 경과 · DB"
                    if active
                    else "종료 시간 미계측",
                    "active": active,
                }
            )
        return {
            "chatCounts": {r["status"]: r["n"] for r in chat},
            "profileCounts": {r["status"]: r["n"] for r in counts},
            "activeCounts": {"chat": active_chat, "profiling": active_profile},
            "jobs": rows,
            "profileLookup": {
                str(r["recipient_user_id"]) + ":" + str(r["source_version"]): {
                    "status": r["status"],
                    "callbackAttempts": r["callback_attempts"],
                }
                for r in profiles
            },
            "budget": {
                "sampleCount": budget["n"],
                "actualUSD": float(budget["actual"])
                if budget["actual"] is not None
                else 0
                if budget["n"] == 0
                else None,
                "unconfirmedReservedUSD": float(budget["reserved"])
                if budget["reserved"] is not None
                else 0
                if budget["unconfirmed_n"] == 0
                else None,
                "scope": "local Chat tables · recent24h only; external shared ledger not represented",
            } if budget is not None else None,
        }

    async def snapshot(self):
        if self.demo:
            return {
                "collectedAt": STAMP(),
                "scope": "합성 모의 프로세스 전체 · 운영 서버 아님",
                "dataMode": "SYNTHETIC",
                "services": {
                    s: {"status": "ready", "source": "built-in fixture"}
                    for s in ["chat", "search", "profiling"]
                },
                "chatCounts": dict(Counter(r["status"] for r in self.turns.values())),
                "profileCounts": dict(
                    Counter(r["status"] for r in self.profiles.values())
                ),
                "jobs": [
                    {
                        "id": self.anon(k),
                        "service": "Chat",
                        "status": r["status"],
                        "created": r["created"],
                    }
                    for k, r in self.turns.items()
                ]
                + [
                    {
                        "id": self.anon(k),
                        "service": "Profiler",
                        "status": r["status"],
                        "created": r["created"],
                        "updated": r["updated"],
                        "callbackAttempts": r["callbackAttempts"],
                    }
                    for k, r in self.profiles.items()
                ],
                "budget": None,
                "unmeasured": [
                    "Server TTFT",
                    "전체 Search 요청 이력",
                    "전체 서비스 비용",
                ],
            }

        async def probe(service, path):
            started = time.perf_counter()
            try:
                status, data = await asyncio.wait_for(
                    self.request(service, "GET", path), timeout=4
                )
                if not isinstance(data, dict):
                    raise TypeError("invalid health")
                policy_matches = self.budget_policy_matches(data, service) if service in {"chat", "profiling"} else True
                data = public_health(service, data)
                ready = status == 200 and (
                    data.get("ready") is True
                    if service == "chat"
                    else data.get("catalog", {}).get("active") is True
                    and data.get("store", {}).get("connected") is True
                    if service == "profiling"
                    else data.get("status") == "ready"
                )
                reasons = []
                if self.config.get("liveModel"):
                    expected_chat = self.config.get("expectedChatModel", self.config.get("expectedModel"))
                    expected_profiler = self.config.get("expectedProfilerModel", self.config.get("expectedModel"))
                    if service == "chat" and (
                        data.get("mode") != "live"
                        or not isinstance(data.get("model"), str)
                        or not data["model"].strip()
                        or (expected_chat and data.get("model") != expected_chat)
                    ):
                        ready = False
                        reasons.append("실제 모드/모델 불일치")
                    if service in {"chat", "profiling"} and not policy_matches:
                        ready = False
                        reasons.append("실제 비용 정책 미확인 또는 설정 불일치")
                    if (
                        service == "search"
                        and self.config.get("searchSnapshotId")
                        and data.get("snapshotId") != self.config["searchSnapshotId"]
                    ):
                        ready = False
                        reasons.append("시작 시점 검색 snapshot 불일치")
                    if (
                        service == "profiling"
                        and self.config.get("catalogVersion")
                        and data.get("catalog", {}).get("version")
                        != self.config["catalogVersion"]
                    ):
                        ready = False
                        reasons.append("시작 시점 카탈로그 generation 불일치")
                    tagger = model_state(data) if service == "profiling" else {}
                    if service == "profiling" and not (
                        tagger.get("enabled") is True
                        and tagger.get("required") is True
                        and isinstance(tagger.get("model"), str)
                        and tagger["model"].strip()
                        and (not expected_profiler or tagger.get("model") == expected_profiler)
                        and data.get("embedding", {}).get("enabled") is True
                    ):
                        ready = False
                        reasons.append("필수 태깅/임베딩 준비 안 됨")
                return service, {
                    "status": "ready" if ready else "unready",
                    "httpStatus": status,
                    "source": path,
                    "data": data,
                    "collectedAt": STAMP(),
                    "probeMs": round((time.perf_counter() - started) * 1000),
                    "reasons": reasons,
                }
            except HTTPException as exc:
                return service, {
                    "status": "unready", "httpStatus": exc.status_code, "source": path,
                    "collectedAt": STAMP(),
                    "reasons": ["공통 BE 토큰을 설정해 주세요." if exc.detail == "SERVICE_TOKEN_UNAVAILABLE"
                                else "서비스 상태 조회를 확인하지 못했습니다."],
                }
            except (
                httpx.HTTPError,
                TimeoutError,
                KeyError,
                ValueError,
                TypeError,
                AttributeError,
            ):
                return service, {
                    "status": "unknown",
                    "source": path,
                    "collectedAt": STAMP(),
                    "reasons": ["상태 응답 수집 실패 · 제한4초"],
                }

        services = dict(
            await asyncio.gather(
                *(
                    probe(service, path)
                    for service, path in [
                        ("chat", "/readyz"),
                        ("search", "/readyz"),
                        ("profiling", "/health"),
                    ]
                )
            )
        )
        services["console"] = {
            "status": "ready",
            "source": "collector process · self report",
            "collectedAt": STAMP(),
            "data": {
                "mode": "LIVE_TARGET"
                if self.config.get("liveModel")
                else "LOCAL_TARGET",
                "sourceRevision": IMPORTED_SERVER_SHA[:12],
                "startedAt": self.started_at,
                "uptimeSeconds": round(time.monotonic() - self.started_monotonic, 1),
            },
        }
        search_metadata = None
        try:
            code, data = await asyncio.wait_for(
                self.request("search", "GET", "/v1/metadata"), timeout=2
            )
            if (
                code == 200
                and isinstance(data, dict)
                and isinstance(data.get("model"), str)
            ):
                search_metadata = {
                    key: data.get(key)
                    for key in (
                        "model",
                        "dimensions",
                        "algorithm",
                        "snapshotId",
                        "productCount",
                    )
                }
        except (httpx.HTTPError, TimeoutError, KeyError, ValueError, TypeError):
            pass
        result = {
            "collectedAt": STAMP(),
            "scope": "로컬 구성요소 준비 상태 · DB 최근24시간 집계 · 작업 각25개(활성 우선)",
            "dataMode": "LOCAL_FIXTURE"
            if self.config.get("localFixtureProfilerFree")
            else "LIVE_SYNTHETIC_INPUT"
            if self.config.get("liveModel")
            else "UNKNOWN",
            "services": services,
            "chatCounts": None,
            "profileCounts": None,
            "jobs": None,
            "activeCounts": None,
            "searchMetadata": search_metadata,
            "searchMetadataError": None
            if search_metadata is not None
            else "model metadata unavailable",
            "expectedModel": self.config.get("expectedModel"),
            "expectedChatModel": self.config.get("expectedChatModel", self.config.get("expectedModel")),
            "expectedProfilerModel": self.config.get("expectedProfilerModel", self.config.get("expectedModel")),
            "catalogVersion": self.config.get("catalogVersion"),
            "sourceManifest": self.config.get("sourceManifest"),
            "budget": None,
            "budgetMode": self.config.get("budgetMode", "provider-key-limits"),
            "budgetPolicyVerified": bool(self.config.get("liveModel")
                and services.get("chat", {}).get("status") == "ready"
                and self.budget_policy_matches(services["chat"].get("data", {}))),
            "unmeasured": ["Server TTFT", "전체 Search 요청 이력", "전체 서비스 비용"],
        }
        shared = services.get("chat", {}).get("data", {}).get("sharedBudget")
        if isinstance(shared, dict):
            result["sharedBudget"] = shared
        if self.config.get("database"):
            try:
                data = await asyncio.to_thread(self.database_snapshot)
                data.pop("profileLookup", None)
                result.update(data, dbSource="read-only PostgreSQL")
            except (psycopg.Error, KeyError, ValueError, TypeError):
                result["dbError"] = "수집 실패 · 이전 숫자를 정상으로 표시하지 않음"
        result["collectionStatus"] = (
            "partial"
            if result.get("dbError")
            or search_metadata is None
            or any(s["status"] == "unknown" for s in services.values())
            else "collected"
        )
        result["collectedAt"] = STAMP()
        result["budgetSeparation"] = {
            "actualUsd": None,
            "unconfirmedUsd": None,
            "reason": "central summary combines actual and uncertain reservations; no fabricated split",
        }
        return result

    def read_profile(self, key):
        recipient, version = map(int, key.split(":"))
        with psycopg.connect(
            self.config["database"], connect_timeout=2, row_factory=dict_row
        ) as db:
            db.execute("SET TRANSACTION READ ONLY")
            db.execute("SET LOCAL statement_timeout='2000ms'")
            row = db.execute(
                "SELECT status,updated_at,callback_attempts,tag_status,llm_cost_usd,error->>'code' error_code FROM ai_profile.profile_runs WHERE recipient_user_id=%s AND source_version=%s",
                (recipient, version),
            ).fetchone()
        return (
            {
                "status": row["status"],
                "updated": row["updated_at"].isoformat(),
                "callbackAttempts": row["callback_attempts"],
                "errorCode": row["error_code"],
                "tagStatus": row["tag_status"],
                "llmCostUsd": float(row["llm_cost_usd"])
                if row["llm_cost_usd"] is not None
                else None,
            }
            if row
            else None
        )

    async def profile_state(self, key):
        recovered = key not in self.profile_inputs
        if recovered and not (
            self.config.get("liveModel")
            and key.split(":")[0] == str(self.config.get("defaultRecipientUserId"))
        ):
            raise HTTPException(
                404, "Only console-submitted or owned synthetic recipient profiles are traceable"
            )
        if self.demo:
            return {
                "source": "built-in fixture state",
                "data": self.profiles.get(key),
                "collectedAt": STAMP(),
            }
        if not self.config.get("database"):
            return {"source": "미수집", "data": None, "collectedAt": STAMP()}
        try:
            data = await asyncio.to_thread(self.read_profile, key)
            if recovered and data is None:
                raise HTTPException(404, "No persisted synthetic profile run")
            return {
                "source": "read-only ai_profile.profile_runs · BE 저장 확인 아님",
                "recoveredObservation": recovered,
                "data": data,
                "collectedAt": STAMP(),
            }
        except (psycopg.Error, KeyError, ValueError, TypeError):
            return {"source": "수집 실패", "data": None, "collectedAt": STAMP()}


async def session_events(admin, request, stream, *, interval=1):
    """Close even an idle upstream stream within one check interval of revocation."""
    pending = None
    try:
        while True:
            admin.session(request)
            if pending is None:
                pending = asyncio.create_task(anext(stream))
            ready, _ = await asyncio.wait({pending}, timeout=interval)
            if not ready:
                continue
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            admin.session(request)
            yield chunk
    except HTTPException:
        return
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()


def create_app(config=None):
    config = dict(config or {})
    for field, variable in (("budgetMode", "AI_BUDGET_MODE"),
                            ("expectedChatModel", "CHAT_MODEL"),
                            ("expectedProfilerModel", "PROFILING_TAGGER_MODEL")):
        value = os.getenv(variable, "").strip()
        if value and field not in config and config.keys() - {"adminAuth"}:
            config[field] = value
    admin = AdminAuth((config or {}).get("adminAuth"))
    try:
        key_source = ProviderKey.from_environment("chat")
    except KeyUnavailable:
        key_source = None
    backend = Backend({k: v for k, v in (config or {}).items() if k != "adminAuth"}, admission_store=admin)
    products = ProductDisplay(config or {}, backend.http)
    provider_usage = ProviderUsage()
    # Keep each process's UI and backend coherent even if the source tree changes.
    assets = {
        name: (ROOT / "static" / name).read_bytes()
        for name in ("index.html", "app.js", "core.js", "ops.js", "products.js", "style.css", "login.html", "login.js", "admin.js", "usage.js")
    }
    build = {
        "serverSha256": IMPORTED_SERVER_SHA,
        "assets": {
            name: hashlib.sha256(data).hexdigest() for name, data in assets.items()
        },
    }
    build_id = hashlib.sha256(json.dumps(build, sort_keys=True).encode()).hexdigest()

    async def observed(operation):
        exchanges = []
        token = HTTP_TRACE.set(exchanges)
        try:
            result = await operation()
            return {
                **result,
                "transport": {
                    "mode": "synthetic"
                    if backend.demo
                    else "attempted"
                    if exchanges
                    else "not-called",
                    "exchanges": exchanges,
                },
            }
        except HTTPException as exc:
            return JSONResponse(
                {
                    "detail": exc.detail,
                    "transport": {
                        "mode": "attempted" if exchanges else "not-called",
                        "exchanges": exchanges,
                    },
                },
                status_code=exc.status_code,
                headers=exc.headers,
            )
        except httpx.HTTPError:
            return JSONResponse(
                {
                    "detail": "Upstream connection failed before a usable response",
                    "transport": {"mode": "attempted", "exchanges": exchanges},
                },
                status_code=502,
            )
        finally:
            HTTP_TRACE.reset(token)

    @asynccontextmanager
    async def lifespan(application):
        try:
            yield
        finally:
            for task in backend.jobs:
                task.cancel()
            await asyncio.gather(*backend.jobs, return_exceptions=True)
            await backend.http.aclose()
            await provider_usage.close()

    app = FastAPI(
        title="AI Local Workbench", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.backend = backend
    app.state.admin = admin
    app.state.provider_key = key_source
    app.state.products = products
    app.state.provider_usage = provider_usage

    @app.middleware("http")
    async def local_boundary(request, call_next):
        try:
            admin.check_origin(request)
            public = ((request.method == "GET" and request.url.path in {
                "/login", "/healthz", "/static/login.js", "/static/style.css"})
                or request.method == "POST" and request.url.path == "/api/admin/login")
            if not public:
                admin.session(request)
            response = await call_next(request)
        except HTTPException as exc:
            if request.url.path == "/" and exc.status_code in {401, 503}:
                response = RedirectResponse("/login", status_code=303)
            else:
                response = JSONResponse({"detail": exc.detail}, exc.status_code, headers=exc.headers)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Console-Build": build_id,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    @app.get("/login")
    async def login_page():
        return Response(assets["login.html"], media_type="text/html")

    @app.get("/healthz")
    async def health():
        try:
            admin.credential()
            configured = True
        except HTTPException:
            configured = False
        return {"alive": True, "adminConfigured": configured, "build": {"id": build_id, **build}}

    @app.post("/api/admin/login")
    async def login(request: Request, response: Response):
        return await admin.login(request, response)

    @app.get("/api/admin/session")
    async def admin_session(request: Request):
        _, session = admin.session(request)
        return {"authenticated": True, "csrf": session["csrf"], "expiresIn": max(0, int(session["expires"]-admin.clock()))}

    @app.post("/api/admin/logout")
    async def logout(request: Request, response: Response):
        return admin.logout(request, response)

    async def credential_state(source, field, services):
        state = await asyncio.to_thread(source.status)
        async def applied(kind):
            if not state["configured"] or state["revision"] is None:
                return "unverified"
            target = "profiling" if kind == "profiler" else "chat"
            try:
                status, data = await backend.request(target, "GET", "/internal/credentials")
                observed = data.get(field, {})
                if status == 200 and data.get("service") == kind and data.get("environment") == state["environment"]:
                    return "observed" if observed.get("configured") is True and observed.get("revision") == state["revision"] else "pending"
            except (HTTPException, httpx.HTTPError, KeyError, TypeError, AttributeError, ValueError):
                pass
            return "unverified"
        state["application"] = dict(zip(services, await asyncio.gather(*(applied(kind) for kind in services))))
        return state

    def unavailable(exc, services):
        return {"mode": "unavailable", "configured": False, "revision": None,
                "state": "source_conflict" if exc.code.endswith("SOURCE_CONFLICT") else "unavailable",
                "verified": False, "writable": False, "environment": environment(),
                "appliesTo": services, "application": {kind: "unverified" for kind in services}}

    @app.get("/api/admin/provider-key")
    @app.get("/api/admin/provider-key/{kind}")
    async def provider_key_status(kind: str = "chat"):
        if kind not in {"chat", "profiler"}:
            raise HTTPException(404, "UNKNOWN_SERVICE")
        try:
            return await credential_state(ProviderKey.from_environment(kind), "provider", [kind])
        except KeyUnavailable as exc:
            return unavailable(exc, [kind])

    @app.get("/api/admin/provider-usage")
    async def provider_usage_status():
        return await provider_usage.status()

    @app.put("/api/admin/provider-key")
    @app.put("/api/admin/provider-key/{kind}")
    async def provider_key_save(request: Request, kind: str = "chat"):
        if kind not in {"chat", "profiler"}:
            raise HTTPException(404, "UNKNOWN_SERVICE")
        body = await private_json(request, {"key", "revision"}, optional={"confirmMigration"})
        if type(body.get("confirmMigration", False)) is not bool:
            raise HTTPException(400, "MIGRATION_CONFIRMATION_INVALID")
        try:
            source = ProviderKey.from_environment(kind)
            saved = await asyncio.to_thread(source.save, body["key"], body["revision"], confirm_migration=body.get("confirmMigration", False))
            return {**saved, "application": {kind: "unverified"}}
        except KeyConflict as exc:
            raise HTTPException(409, exc.code) from None
        except KeyUnavailable as exc:
            raise HTTPException(400 if exc.code == "PROVIDER_KEY_INVALID" else 503, exc.code) from None

    @app.get("/api/admin/backend-token")
    async def service_token_status():
        try:
            return await credential_state(ServiceToken.from_environment("chat"), "backend", ["chat", "profiler"])
        except KeyUnavailable as exc:
            return unavailable(exc, ["chat", "profiler"])

    @app.put("/api/admin/backend-token")
    async def service_token_save(request: Request):
        body = await private_json(request, {"key", "revision", "confirmShared"})
        if type(body["confirmShared"]) is not bool:
            raise HTTPException(400, "SHARED_TOKEN_CONFIRMATION_INVALID")
        try:
            source = ServiceToken.from_environment("chat")
            saved = await asyncio.to_thread(source.save, body["key"], body["revision"],
                                            confirm_shared=body["confirmShared"])
            return {**saved, "application": {"chat": "unverified", "profiler": "unverified"}}
        except KeyConflict as exc:
            raise HTTPException(409, exc.code) from None
        except KeyUnavailable as exc:
            raise HTTPException(400 if exc.code == "SERVICE_TOKEN_INVALID" else 503, exc.code) from None

    @app.get("/")
    async def home():
        return Response(assets["index.html"], media_type="text/html")

    @app.get("/api/config")
    async def public_config():
        chat_allowed = True
        profile_allowed = backend.demo or bool(backend.config.get("localFixtureProfilerFree") and backend.config.get("launcherPid"))
        if backend.config.get("liveModel"):
            async def allowed(check):
                try:
                    await check()
                    return True
                except (HTTPException, httpx.HTTPError, psycopg.Error, KeyError, TypeError, ValueError):
                    return False
            chat_allowed, profile_allowed = await asyncio.gather(allowed(backend.check_live_budget), allowed(backend.check_profile_ready))
        return {
            "mode": "DEMO" if backend.demo else "LOCAL_TARGET",
            "targetLabel": backend.config.get("label", "내장 합성 대상"),
            "chatSendAllowed": chat_allowed,
            "profileSendAllowed": profile_allowed,
            "paidCallsEnabled": bool(backend.config.get("liveModel")),
            "budgetMode": backend.config.get("budgetMode", "provider-key-limits"),
            "budgetPolicyVerified": bool(backend.config.get("liveModel") and chat_allowed),
            "defaultRecipientUserId": backend.config.get("defaultRecipientUserId", 42),
            "defaultSenderUserId": backend.config.get("defaultSenderUserId", 7),
            "chatRecipients": backend.config.get("chatRecipients", []),
            "build": {"id": build_id, **build},
        }

    @app.post("/api/chat")
    async def chat(body: ChatInput, request: Request):
        key, _ = admin.session(request)
        return await observed(lambda: backend.chat(body, key))

    @app.get("/api/chat/{tid}")
    async def state(tid: uuid.UUID, request: Request):
        key, _ = admin.session(request)
        return await observed(lambda: backend.state(str(tid), key))

    @app.put("/api/chat/{tid}/cancellation")
    async def cancel(tid: uuid.UUID, request: Request):
        key, _ = admin.session(request)
        return await observed(lambda: backend.cancel(str(tid), key))

    @app.get("/api/chat/{tid}/events")
    async def events(tid: uuid.UUID, request: Request, afterEventId: int = 0):
        key, _ = admin.session(request)
        backend.admission(str(tid), key)
        return StreamingResponse(
            session_events(admin, request, backend.events(str(tid), max(0, afterEventId), key)),
            media_type="text/event-stream",
        )

    @app.post("/api/profile")
    async def profile(body: ProfileInput):
        return await observed(lambda: backend.profile(body))

    @app.get("/api/profile/{recipient}/{version}")
    async def profile_state(recipient: int, version: int):
        return await observed(
            lambda: backend.profile_state(str(recipient) + ":" + str(version))
        )

    @app.get("/api/snapshot")
    async def snapshot():
        return await backend.snapshot()

    @app.get("/api/products/{ident}/image")
    async def product_image(ident: int):
        data, media_type = await products.image(ident)
        return Response(data, media_type=media_type)

    @app.get("/api/products/{ident}")
    async def product_metadata(ident: int):
        return await products.metadata(ident)

    @app.get("/static/{filename}")
    async def static(filename: str):
        if filename not in assets:
            raise HTTPException(404, "Unknown static asset")
        mime = (
            "text/javascript"
            if filename.endswith(".js")
            else "text/css"
            if filename.endswith(".css")
            else "text/html"
        )
        return Response(assets[filename], media_type=mime)

    return app


config = (
    json.loads(Path(os.environ["CONSOLE_TARGET_FILE"]).read_text())
    if os.getenv("CONSOLE_TARGET_FILE")
    else None
)
app = create_app(config)
