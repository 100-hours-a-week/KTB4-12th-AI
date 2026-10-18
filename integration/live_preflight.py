"""Pure local safety checks before any live-validation process starts."""

import json
import math
import re
import sqlite3
from pathlib import Path
from uuid import UUID

ACCOUNT = "gift-chat-harness-2026-10-06"
CREDENTIAL_NAMES = (
    "STORE_TOKEN",
    "AI_SERVICE_TOKEN",
    "SESSION_SECRET",
    "QA_ADMIN_PASSWORD",
    "DEMO_ACCESS_TOKEN",
)


def validate_credentials(values):
    if any(
        not isinstance(values.get(key), str) or not values[key].strip()
        for key in CREDENTIAL_NAMES
    ):
        raise ValueError("Existing nonblank named credentials required")


def validate_profiler_auth(variables):
    required = variables.get("PROFILING_TAGGER_REQUIRED", "").lower() == "true"
    if required or variables.get("PROFILING_TAGGER") == "openrouter":
        token = variables.get("PROFILING_SERVICE_TOKEN")
        if not isinstance(token, str) or not token.strip():
            raise ValueError(
                "Live/required Profiler needs an explicit nonblank service token"
            )


def validate_resume(run, qa, supplied_fence, max_attempts, max_reserved):
    """Require the original state binding; never create a directory/file here."""
    run, qa = Path(run).resolve(), Path(qa).resolve()
    try:
        saved = json.loads((run / "runtime.json").read_text())
        state = json.loads((run / "backend-state.json").read_text())
        if (
            saved["mode"] != "LIVE"
            or saved["existingBudgetAccount"] != ACCOUNT
            or Path(saved["ownedQA"]).resolve() != qa
        ):
            raise ValueError("Resume QA/account binding differs")
        if (
            saved["maxNewAttempts"] != max_attempts
            or saved["maxNewReservationUsd"] != max_reserved
        ):
            raise ValueError("Resume must retain the recorded validation guard")
        fence = Path(saved["budgetStateFile"]).resolve()
        if supplied_fence is not None and Path(supplied_fence).resolve() != fence:
            raise ValueError("Resume cannot switch its preserved validation fence")
        if not fence.is_file():
            raise ValueError("Preserved validation fence missing")
        with sqlite3.connect(fence.as_uri() + "?mode=ro", uri=True) as db:
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("Invalid preserved validation fence")
            rows = db.execute("SELECT amount,model,ack FROM attempts").fetchall()
            if any(
                type(amount) not in (int, float)
                or not math.isfinite(amount)
                or amount <= 0
                or not isinstance(model, str)
                or ack not in (0, 1)
                for amount, model, ack in rows
            ):
                raise ValueError("Invalid preserved reservation")
        if (
            state.get("synthetic") is not True
            or state["recipientUserId"] != saved["recipientUserId"]
            or type(state["sourceVersion"]) is not int
            or state["sourceVersion"] < saved["sourceVersion"]
        ):
            raise ValueError("Resume callback recipient/version differs")
        if state["sourceVersion"] != saved["sourceVersion"]:
            binding = state.get("profileInputs", {}).get(str(state["sourceVersion"]), {})
            if (not re.fullmatch(r"[a-f0-9]{64}", binding.get("fingerprint", ""))
                or binding.get("giftPreference") != state.get("giftPreference")
                or binding.get("dislikedCategories") != state.get("dislikedCategories")
                or state.get("callback") is None):
                raise ValueError("Resume advanced version needs matching committed input binding")
        if "callback" not in state:
            raise ValueError("Resume callback state missing")
        if (
            "giftPreference" not in state
            or "dislikedCategories" not in state
            or not isinstance(state["dislikedCategories"], list)
        ):
            raise ValueError("Resume preference snapshot missing")
        callback = state["callback"]
        if callback is not None:
            if (
                not isinstance(callback, dict)
                or callback.get("recipientUserId") != saved["recipientUserId"]
                or callback.get("sourceVersion") != state["sourceVersion"]
                or callback.get("profileStatus") != "COMPLETED"
            ):
                raise ValueError("Resume callback payload differs")
            ids = callback.get("recommendedProductIds")
            if (
                not isinstance(ids, list)
                or len(ids) > 30
                or any(type(ident) is not int or ident <= 0 for ident in ids)
                or len(set(ids)) != len(ids)
            ):
                raise ValueError("Resume callback candidate list invalid")
        if (
            state["callback"] is not None
            and state.get("snapshotId") != saved["searchSnapshotId"]
        ):
            raise ValueError("Resume callback snapshot differs")
        generation = (qa / "search-root/data/CURRENT").read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", generation):
            raise ValueError("Invalid generation pointer")
        catalog = json.loads(
            (
                qa / "search-root/data/generations" / generation / "catalog.json"
            ).read_text()
        )
        if (
            catalog["ai_catalog_version"] != saved["catalogVersion"]
            or len(catalog["products"]) != saved["catalogProducts"]
        ):
            raise ValueError("Resume catalog generation differs")
        identity = saved.get("databaseIdentity")
        if not identity:
            identity = json.loads((qa / "provenance.json").read_text())["runIdentity"]
        UUID(identity)
        return fence, identity
    except (OSError, KeyError, TypeError, sqlite3.Error, json.JSONDecodeError) as exc:
        raise ValueError(
            "Resume requires intact runtime, callback, catalog and preserved fence"
        ) from exc
