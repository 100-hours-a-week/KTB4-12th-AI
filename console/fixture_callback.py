"""Loopback synthetic BE stand-in for the Console fixture: RabbitMQ request publish + durable result log.

11-08: the 7.6 webhook / 7.7 callback were replaced by RabbitMQ. This process plays the BE: it binds a synthetic
input, publishes ``profiling.request`` and records every ``profiling.result`` per recipient across restarts.
It runs on the Profiler environment (pika) and uses only the disposable broker in CONSOLE_FIXTURE_AMQP_URL.
(The file keeps its old name so existing fixture state paths stay valid.)
"""

import contextlib
import hashlib
import json
import os
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from integration.profile_contract import ProfileInput
from integration.tests import mq_harness

lock = threading.Lock()
state_path = Path(os.environ["CONSOLE_FIXTURE_CALLBACK_STATE"])
AMQP_URL = os.environ["CONSOLE_FIXTURE_AMQP_URL"]


def read():
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    state.setdefault("calls", {})          # recipient → received result payloads
    state.setdefault("bindings", {})       # "recipient:version" → input fingerprint
    return state


def write(state):
    temporary = state_path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    temporary.replace(state_path)


def record(message):
    payload = message.get("payload") or {}
    with lock:
        state = read()
        state["calls"].setdefault(str(payload.get("recipientUserId")), []).append(payload)
        write(state)


@contextlib.asynccontextmanager
async def lifespan(_app):
    mq_harness.declare(AMQP_URL, purge=False)
    stop = threading.Event()

    def consume():
        while not stop.is_set():
            try:
                with mq_harness.Results(AMQP_URL) as results:
                    seen = 0
                    while not stop.is_set():
                        items = list(results.items)
                        for message in items[seen:]:
                            record(message)
                        seen = len(items)
                        stop.wait(0.1)
            except Exception:  # noqa: BLE001 — reconnect to the synthetic broker
                stop.wait(1)

    worker = threading.Thread(target=consume, name="fixture-results", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(5)


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"synthetic": True, "persistent": True, "transport": "rabbitmq"}


def fingerprint(body: ProfileInput):
    return hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()


@app.put("/fixture/recipients/{recipient}/profile-input")
def bind(recipient: int, body: ProfileInput):
    if body.recipientUserId != recipient:
        return JSONResponse({"message": "Synthetic recipient mismatch"}, status_code=400)
    key = f"{recipient}:{body.sourceVersion}"
    with lock:
        state = read()
        old = state["bindings"].get(key)
        if old and old != fingerprint(body):
            return JSONResponse({"message": "Same recipient/version requires identical input"}, status_code=409)
        state["bindings"][key] = fingerprint(body)
        write(state)
    return {"synthetic": True, "sourceVersion": body.sourceVersion, "inputBound": True}


@app.post("/fixture/recipients/{recipient}/profile-request")
def request_profile(recipient: int, body: ProfileInput):
    with lock:
        bound = read()["bindings"].get(f"{recipient}:{body.sourceVersion}")
    if body.recipientUserId != recipient or bound != fingerprint(body):
        return JSONResponse({"message": "Bind the same input first"}, status_code=409)
    return {"synthetic": True, "published": True,
            "requestId": mq_harness.publish(AMQP_URL, body.model_dump())}


@app.get("/fixture/calls")
def calls():
    with lock:
        return read()["calls"]
