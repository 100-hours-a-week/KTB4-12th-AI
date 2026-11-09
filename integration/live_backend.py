"""Synthetic BE-contract harness: RabbitMQ request publish + durable result ingestion -> next Chat snapshot.

Not the team's Backend. Never reads real preferences or changes a BE repository.
11-08: the 7.6 webhook / 7.7 callback were replaced by RabbitMQ. Like the BE, this harness declares the
synthetic topology, publishes ``profiling.request`` and consumes ``profiling.result`` (when LIVE_BACKEND_AMQP_URL is set).
"""

import contextlib
import hashlib
import hmac
import json
import logging
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from integration.profile_contract import ProfileInput

STATE = Path(os.environ["LIVE_BACKEND_STATE"])
TOKEN = os.environ["AI_SERVICE_TOKEN"]
SEARCH = os.environ["SEARCH_URL"]
AMQP_URL = os.environ.get("LIVE_BACKEND_AMQP_URL", "")
LOCK = threading.Lock()
log = logging.getLogger("live_backend")

# BE topology (BE-owned in deployment; declared here only for the synthetic broker).
EXCHANGE = "profiling.exchange"
REQUEST, RESULT, FAILED = "profiling.request", "profiling.result", "profiling.request.failed"


@contextlib.asynccontextmanager
async def lifespan(_app):
    stop, worker = threading.Event(), None
    if AMQP_URL:
        declare_topology()
        worker = threading.Thread(target=consume_results, args=(stop,), name="synthetic-result-consumer", daemon=True)
        worker.start()
    try:
        yield
    finally:
        stop.set()
        if worker is not None:
            worker.join(5)


app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)


def authorized(request):
    return hmac.compare_digest(
        request.headers.get("authorization", ""), "Bearer " + TOKEN
    )


def save_state(state):
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False))
    temp.chmod(0o600)
    temp.replace(STATE)


@app.put("/fixture/recipients/{recipient}/profile-input")
def bind_profile_input(recipient: int, body: ProfileInput, request: Request):
    """Bind synthetic BE input before model admission, without changing committed context."""
    if not authorized(request):
        return JSONResponse({"message": "Unauthorized"}, status_code=401)
    with LOCK:
        state = json.loads(STATE.read_text())
        if state.get("synthetic") is not True or recipient != state["recipientUserId"] or body.recipientUserId != recipient:
            return JSONResponse({"message": "Synthetic recipient mismatch"}, status_code=400)
        if body.sourceVersion < state["sourceVersion"]:
            return JSONResponse({"message": "Stale source version"}, status_code=409)
        # Keep only context fields and a full-body digest; never persist review text here.
        fingerprint = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
        bindings = state.setdefault("profileInputs", {})
        key = str(body.sourceVersion)
        old = bindings.get(key)
        if old and old["fingerprint"] != fingerprint:
            return JSONResponse({"message": "Same recipient/version requires identical input"}, status_code=409)
        if not old and body.sourceVersion == state["sourceVersion"] and state.get("callback"):
            return JSONResponse({"message": "Legacy completed input needs verified binding before reuse"}, status_code=409)
        if not old and len(bindings) >= 256:
            return JSONResponse({"message": "Synthetic input history limit reached"}, status_code=429)
        bindings[key] = {"fingerprint": fingerprint, "giftPreference": body.giftPreference, "dislikedCategories": body.model_dump()["dislikedCategories"]}
        save_state(state)
    return {"synthetic": True, "sourceVersion": body.sourceVersion, "inputBound": True}


def apply_result(recipient: int, body: dict):
    """Validate and persist one successful profiling result (old 7.7 body shape). Returns (status, message)."""
    with LOCK:
        state = json.loads(STATE.read_text())
        if (
            state.get("synthetic") is not True
            or recipient != state["recipientUserId"]
            or body.get("recipientUserId") != recipient
        ):
            return 400, "Synthetic recipient mismatch"
        version = body.get("sourceVersion")
        binding = state.get("profileInputs", {}).get(str(version))
        if type(version) is not int or version < state["sourceVersion"] or (version != state["sourceVersion"] and not binding):
            return 409, "Stale or unbound source version"
        ids = body.get("recommendedProductIds", [])
        if (
            set(body)
            != {
                "recipientUserId",
                "sourceVersion",
                "profileStatus",
                "recommendedProductIds",
            }
            or body.get("profileStatus") != "COMPLETED"
            or not isinstance(ids, list)
            or any(type(ident) is not int or ident <= 0 for ident in ids)
            or len(ids) > 30
            or len(set(ids)) != len(ids)
        ):
            return 400, "Invalid result"
        with httpx.Client(trust_env=False, timeout=10, headers={"Authorization": "Bearer " + TOKEN}) as http:
            meta = http.get(SEARCH + "/v1/metadata")
            meta.raise_for_status()
            snapshot = meta.json()["snapshotId"]
            products = (
                http.post(
                    SEARCH + "/v1/products", json={"ids": ids, "snapshotId": snapshot}
                )
                if ids
                else None
            )
            if products is not None:
                products.raise_for_status()
            if products is not None and {
                p["productId"] for p in products.json()["products"]
            } != set(ids):
                return 400, "Unknown Backend product ID"
        if binding:
            state.update(sourceVersion=version, giftPreference=binding["giftPreference"], dislikedCategories=binding["dislikedCategories"])
        state["callback"] = body
        state["snapshotId"] = snapshot
        save_state(state)
    return 200, "Synthetic BE contract result persisted"


def handle_result(raw: bytes) -> int:
    """One ``profiling.result`` message → state. FAILED outcomes are recorded only; nothing is retried here."""
    try:
        doc = json.loads(raw)
        payload = doc["payload"]
        recipient, version, outcome = payload["recipientUserId"], payload["sourceVersion"], payload["outcome"]
    except (ValueError, TypeError, KeyError):
        return 400
    if type(recipient) is not int or doc.get("schemaVersion") != 1:
        return 400
    if outcome == "FAILED":
        with LOCK:
            state = json.loads(STATE.read_text())
            state["lastFailure"] = {"sourceVersion": version, "code": (payload.get("error") or {}).get("code")}
            save_state(state)
        return 200
    data = payload.get("data")
    if outcome != "SUCCEEDED" or not isinstance(data, dict) or set(data) != {"recommendedProductIds"}:
        return 400
    status, _ = apply_result(recipient, {"recipientUserId": recipient, "sourceVersion": version,
                                         "profileStatus": "COMPLETED", "recommendedProductIds": data["recommendedProductIds"]})
    return status


def _channel():
    import pika
    connection = pika.BlockingConnection(pika.URLParameters(AMQP_URL))
    return connection, connection.channel()


def declare_topology():
    connection, channel = _channel()
    try:
        channel.exchange_declare(EXCHANGE, "direct", durable=True, auto_delete=False)
        for queue in (REQUEST, RESULT, FAILED):
            channel.queue_declare(queue, durable=True, arguments={"x-queue-type": "quorum"})
            channel.queue_bind(queue, EXCHANGE, routing_key=queue)
    finally:
        connection.close()


def publish_request(body: ProfileInput) -> str:
    """Publish like the BE: persistent JSON/UTF-8, message_id = requestId, mandatory, publisher confirm."""
    import pika
    request_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    message = {"schemaVersion": 1, "requestId": request_id,
               "createdAt": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
               "payload": body.model_dump()}
    connection, channel = _channel()
    try:
        channel.confirm_delivery()
        channel.basic_publish(EXCHANGE, REQUEST, json.dumps(message, ensure_ascii=False).encode(),
                              pika.BasicProperties(content_type="application/json", content_encoding="UTF-8",
                                                   delivery_mode=2, message_id=request_id), mandatory=True)
    finally:
        connection.close()
    return request_id


def consume_results(stop: threading.Event):
    while not stop.is_set():
        try:
            connection, channel = _channel()
        except Exception:  # noqa: BLE001 — broker not up yet; retry quietly
            stop.wait(1)
            continue
        try:
            channel.basic_qos(prefetch_count=1)
            for method, _, raw in channel.consume(RESULT, inactivity_timeout=0.5):
                if stop.is_set():
                    break
                if method is None:
                    continue
                status = handle_result(raw)
                if status != 200:
                    log.warning("synthetic result rejected (%s)", status)
                channel.basic_ack(method.delivery_tag)
        except Exception:  # noqa: BLE001
            log.exception("synthetic result consumer reconnecting")
        finally:
            with contextlib.suppress(Exception):
                connection.close()



@app.post("/fixture/recipients/{recipient}/profile-request")
def request_profile(recipient: int, body: ProfileInput, request: Request):
    """Publish a bound synthetic input to ``profiling.request`` (the BE's role)."""
    if not authorized(request):
        return JSONResponse({"message": "Unauthorized"}, status_code=401)
    if not AMQP_URL:
        return JSONResponse({"message": "Synthetic broker not configured"}, status_code=503)
    fingerprint = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    with LOCK:
        state = json.loads(STATE.read_text())
        binding = state.get("profileInputs", {}).get(str(body.sourceVersion))
    if recipient != state["recipientUserId"] or body.recipientUserId != recipient:
        return JSONResponse({"message": "Synthetic recipient mismatch"}, status_code=400)
    if not binding or binding["fingerprint"] != fingerprint:
        return JSONResponse({"message": "Bind the same input first"}, status_code=409)
    return {"synthetic": True, "requestId": publish_request(body), "published": True}


@app.get("/fixture/recipients/{recipient}/chat-context")
def context(recipient: int, request: Request):
    if not authorized(request):
        return JSONResponse({"message": "Unauthorized"}, status_code=401)
    state = json.loads(STATE.read_text())
    if recipient != state["recipientUserId"]:
        return JSONResponse({"message": "Unknown synthetic recipient"}, status_code=404)
    if not state.get("callback"):
        return {
            "synthetic": True,
            "recipientContext": {
                "recipientUserId": recipient,
                "status": "NONE",
                "sourceVersion": 0,
                "giftPreference": None,
                "dislikedCategories": [],
            },
        }
    has_preferences = bool(
        (state.get("giftPreference") or "").strip() or state.get("dislikedCategories")
    )
    return {
        "synthetic": True,
        "recipientContext": {
            "recipientUserId": recipient,
            "status": "AVAILABLE" if has_preferences else "NONE",
            "sourceVersion": state["sourceVersion"],
            "giftPreference": state["giftPreference"] if has_preferences else None,
            "dislikedCategories": state["dislikedCategories"]
            if has_preferences
            else [],
        },
        **(
            {
                "preparedContext": {
                    "productIds": state["callback"]["recommendedProductIds"],
                    "snapshotId": state["snapshotId"],
                }
            }
            if state["callback"]["recommendedProductIds"]
            else {}
        ),
    }
