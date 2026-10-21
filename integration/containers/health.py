"""Container role readiness checks; never makes model calls."""

import json
import os
import sys
import urllib.request
from pathlib import Path


def service_token():
    from integration.containers.entrypoint import secret
    try:
        return secret("AI_SERVICE_TOKEN")
    except (OSError, ValueError):
        raise SystemExit("AI service token file or variable missing") from None


def main():
    role = sys.argv[1]
    endpoints = {
        "profiler": ("http://127.0.0.1:8001/readyz", False),
        "search": ("http://127.0.0.1:8000/readyz", False),
        "chat": ("http://127.0.0.1:7860/readyz", True),
        "backend": ("http://127.0.0.1:8002/fixture/recipients/42/chat-context", True),
    }
    url, auth = endpoints[role]
    headers = {}
    from integration.service_token import shared_token_configured
    if auth and role == "chat" and (shared_token_configured() or os.getenv("CHAT_BE_SERVICE_TOKEN_FILE")):
        from integration.service_token import expected_token
        headers["Authorization"] = "Bearer " + expected_token("chat")
    elif auth:
        headers["Authorization"] = "Bearer " + service_token()
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        urllib.request.Request(url, headers=headers), timeout=3
    ) as response:
        data = json.load(response)
    if role == "profiler" and data.get("ready") is not True:
        raise SystemExit(1)
    if role == "search" and data.get("status") != "ready":
        raise SystemExit(1)
    if role == "chat" and data.get("ready") is not True:
        raise SystemExit(1)
    if role == "backend" and data.get("synthetic") is not True:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
