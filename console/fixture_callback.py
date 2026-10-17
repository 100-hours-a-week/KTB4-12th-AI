"""Loopback synthetic callback with durable calls/modes across fixture restarts."""

import json
import os
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()
lock = threading.Lock()
state_path = Path(os.environ["CONSOLE_FIXTURE_CALLBACK_STATE"])


def read():
    return (
        json.loads(state_path.read_text())
        if state_path.exists()
        else {"calls": {}, "modes": {}}
    )


def write(state):
    temporary = state_path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    temporary.replace(state_path)


@app.get("/health")
def health():
    return {"synthetic": True, "persistent": True}


@app.put("/fixture/mode/{recipient}/{status}")
def mode(recipient: int, status: int):
    if status not in {200, 400, 429, 503}:
        return JSONResponse({"error": "Unsupported synthetic status"}, status_code=422)
    with lock:
        state = read()
        state["modes"][str(recipient)] = status
        write(state)
    return {"synthetic": True}


@app.get("/fixture/calls")
def calls():
    with lock:
        return read()["calls"]


@app.post("/api/internal/v1/recipients/{recipient}/profile")
def callback(recipient: int, body: dict):
    with lock:
        state = read()
        state["calls"].setdefault(str(recipient), []).append(body)
        status = state["modes"].get(str(recipient), 200)
        write(state)
    return JSONResponse(
        {"message": "synthetic callback", "error": {"code": "FIXTURE"}},
        status_code=status,
    )
