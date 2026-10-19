"""Container role readiness checks; never makes model calls."""

import json
import os
import sys
import urllib.request
from pathlib import Path


def service_token():
    # Same order as the launcher: the configured secret file, else the injected variable.
    path = os.environ.get("AI_SERVICE_TOKEN_FILE")
    token = Path(path).read_text().strip() if path else os.environ.get("AI_SERVICE_TOKEN", "").strip()
    if not token:
        raise SystemExit("AI service token file or variable missing")
    return token


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
    if auth:
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
