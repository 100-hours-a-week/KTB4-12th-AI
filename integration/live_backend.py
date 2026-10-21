"""Synthetic BE-contract harness: durable callback ingestion -> next Chat snapshot.

Not the team's Backend. Never reads real preferences or changes a BE repository.
"""

import hashlib
import hmac
import json
import os
import threading
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from integration.profile_contract import ProfileInput

app = FastAPI(docs_url=None, redoc_url=None)
STATE = Path(os.environ["LIVE_BACKEND_STATE"])
TOKEN = os.environ["AI_SERVICE_TOKEN"]
SEARCH = os.environ["SEARCH_URL"]
LOCK = threading.Lock()


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


@app.post("/api/internal/v1/recipients/{recipient}/profile")
def callback(recipient: int, body: dict, request: Request):
    if not authorized(request):
        return JSONResponse({"message": "Unauthorized"}, status_code=401)
    with LOCK:
        state = json.loads(STATE.read_text())
        if (
            state.get("synthetic") is not True
            or recipient != state["recipientUserId"]
            or body.get("recipientUserId") != recipient
        ):
            return JSONResponse(
                {"message": "Synthetic recipient mismatch"}, status_code=400
            )
        version = body.get("sourceVersion")
        binding = state.get("profileInputs", {}).get(str(version))
        if type(version) is not int or version < state["sourceVersion"] or (version != state["sourceVersion"] and not binding):
            return JSONResponse({"message": "Stale or unbound source version"}, status_code=409)
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
            return JSONResponse({"message": "Invalid callback"}, status_code=400)
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
                return JSONResponse(
                    {"message": "Unknown Backend product ID"}, status_code=400
                )
        if binding:
            state.update(sourceVersion=version, giftPreference=binding["giftPreference"], dislikedCategories=binding["dislikedCategories"])
        state["callback"] = body
        state["snapshotId"] = snapshot
        save_state(state)
    return {"message": "Synthetic BE contract callback persisted", "synthetic": True}


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
