"""Container role readiness checks; never makes model calls."""

import json
import os
import sys
import urllib.request
from pathlib import Path


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
        headers["Authorization"] = (
            "Bearer " + Path(os.environ["AI_SERVICE_TOKEN_FILE"]).read_text().strip()
        )
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
