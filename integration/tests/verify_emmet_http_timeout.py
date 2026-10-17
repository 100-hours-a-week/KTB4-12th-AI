"""Real HTTPTransport read timeout on an owned loopback fixture; no external provider call."""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from integration.profiler_boundary import BudgetedExtractor
from profiling.ports import TagExtractError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    release = threading.Event()
    received = threading.Event()
    seen = []
    settlements = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            received.set()
            release.wait(1.5)
            body = json.dumps(
                {
                    "choices": [
                        {"message": {"content": '{"tags":[]}'}, "finish_reason": "stop"}
                    ],
                    "usage": {"cost": 0.001},
                }
            ).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    class Budget:
        def reserve(self, body):
            return body | {"max_tokens": 1200}, "synthetic-attempt"

        def finish(self, ident, cost):
            settlements.append((ident, cost))

        def close(self):
            pass

    class LoopbackOnly(httpx.BaseTransport):
        def __init__(self):
            self.real = httpx.HTTPTransport(retries=0)

        def handle_request(self, request):
            seen.append(dict(request.extensions))
            # Test transport maps the approved synthetic wire to loopback. No provider address is opened.
            local = httpx.Request(
                "POST",
                f"http://127.0.0.1:{server.server_port}/silent",
                content=request.content,
                headers={"Content-Type": "application/json"},
                extensions=dict(request.extensions),
            )
            return self.real.handle_request(local)

        def close(self):
            self.real.close()

    extractor = BudgetedExtractor(
        "https://openrouter.ai/api/v1",
        "synthetic-not-a-real-key",
        "openai/gpt-6-luna",
        budget=Budget(),
        timeout_s=0.123,
        transport=LoopbackOnly(),
    )
    started = time.monotonic()
    kind = None
    try:
        try:
            extractor.extract("SYNTHETIC", "SYNTHETIC", {})
        except TagExtractError as error:
            kind = error.kind
        elapsed = time.monotonic() - started
        assert kind == "timeout", kind
        assert received.is_set(), "Fixture must receive actual socket request"
        assert 0.07 <= elapsed < 1.0, elapsed
        assert seen == [
            {
                "timeout": {
                    "connect": 0.123,
                    "read": 0.123,
                    "write": 0.123,
                    "pool": 0.123,
                }
            }
        ], seen
        assert settlements == [("synthetic-attempt", None)]
        result = {
            "synthetic": True,
            "configuredTimeoutSeconds": 0.123,
            "forwardedExtensions": seen,
            "realHTTPTransportUsed": True,
            "actualLoopbackRequestReceived": True,
            "timeoutKind": kind,
            "elapsedSeconds": elapsed,
            "serverDelaySeconds": 1.5,
            "unknownReservationRetained": True,
            "externalProviderCalls": 0,
            "realLedgerWrites": 0,
            "passed": True,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    finally:
        release.set()
        extractor.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


if __name__ == "__main__":
    main()
