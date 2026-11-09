"""Synthetic RabbitMQ side of the local harnesses — the BE's role: topology, request publish, result consume.

11-08: the 7.6 webhook / 7.7 callback were replaced by RabbitMQ, so local scripts no longer run a fake
callback server. They publish ``profiling.request`` and read ``profiling.result`` here instead.

Only a disposable local broker named by ``INTEGRATION_TEST_AMQP_URL`` (amqp://…/gift) is used. Never point it
at a shared broker: ``declare`` purges the three profiling queues. ``require_local`` enforces it — any host
other than a local test broker (localhost · 127.x · ::1 · compose ``rabbitmq`` · host.docker.internal) is refused.
"""
import contextlib
import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

EXCHANGE = "profiling.exchange"
REQUEST, RESULT, FAILED = "profiling.request", "profiling.result", "profiling.request.failed"

# Small synthetic numbers (deployment values are an operator decision; MQ config refuses missing ones).
PROFILER_NUMBERS = {
    "PROFILING_MQ_PREFETCH": "2", "PROFILING_MQ_SLOTS": "2", "PROFILING_MQ_RECONNECT_DELAY_S": "1",
    "PROFILING_MQ_SHUTDOWN_GRACE_S": "5", "PROFILING_MQ_CONFIRM_TIMEOUT_S": "5", "PROFILING_MQ_WAIT_S": "1",
    "PROFILING_MQ_MAX_ANALYSIS_ATTEMPTS": "3", "PROFILING_MQ_RETRY_BASE_S": "1", "PROFILING_MQ_RETRY_MAX_S": "5",
    "PROFILING_MQ_RECONCILE_AFTER_S": "60", "PROFILING_MQ_RECONCILE_INTERVAL_S": "30",
}


LOCAL_HOSTS = frozenset({"localhost", "::1", "rabbitmq", "host.docker.internal"})


def require_local(url: str) -> str:
    """Refuse any broker that is not a disposable local one — declare() purges the profiling queues."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        host = ""
    if host in LOCAL_HOSTS or host.startswith("127.") or host.endswith((".localhost", ".invalid")):
        return url
    raise RuntimeError("INTEGRATION_TEST_AMQP_URL must name a local test broker (localhost · 127.x · ::1 · rabbitmq · "
                       "host.docker.internal) — the harness purges the profiling queues, so shared brokers are refused")


def broker_url() -> str:
    url = os.environ.get("INTEGRATION_TEST_AMQP_URL", "")
    if not url.startswith(("amqp://", "amqps://")):
        raise RuntimeError("INTEGRATION_TEST_AMQP_URL (disposable local RabbitMQ, vhost gift) is required")
    return require_local(url)


def profiler_env(url: str) -> dict:
    return {"PROFILING_MQ_ENABLED": "true", "PROFILING_MQ_URL": url, **PROFILER_NUMBERS}


def _channel(url: str):
    import pika  # lazy: launchers on venvs without pika still read broker_url / profiler_env
    connection = pika.BlockingConnection(pika.URLParameters(require_local(url)))
    return connection, connection.channel()


def declare(url: str, *, purge: bool = True) -> None:
    """Declare the BE-owned topology on the synthetic broker (quorum, durable, same-name routing keys)."""
    connection, channel = _channel(url)
    try:
        channel.exchange_declare(EXCHANGE, "direct", durable=True, auto_delete=False)
        for queue in (REQUEST, RESULT, FAILED):
            channel.queue_declare(queue, durable=True, arguments={"x-queue-type": "quorum"})
            channel.queue_bind(queue, EXCHANGE, routing_key=queue)
            if purge:
                channel.queue_purge(queue)
    finally:
        connection.close()


def publish(url: str, body: dict) -> str:
    """Publish one request like the BE: persistent JSON/UTF-8, message_id = requestId, mandatory, confirm."""
    request_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    message = {"schemaVersion": 1, "requestId": request_id,
               "createdAt": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z", "payload": body}
    import pika
    connection, channel = _channel(url)
    try:
        channel.confirm_delivery()
        channel.basic_publish(EXCHANGE, REQUEST, json.dumps(message, ensure_ascii=False).encode(),
                              pika.BasicProperties(content_type="application/json", content_encoding="UTF-8",
                                                   delivery_mode=2, message_id=request_id), mandatory=True)
    finally:
        connection.close()
    return request_id


class Results:
    """Collect ``profiling.result`` messages in a background thread (ACK after parsing)."""

    def __init__(self, url: str) -> None:
        self.url, self.items, self._stop = url, [], threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="synthetic-results", daemon=True)

    def __enter__(self) -> "Results":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(5)

    def _run(self) -> None:
        connection, channel = _channel(self.url)
        try:
            for method, _, raw in channel.consume(RESULT, inactivity_timeout=0.2):
                if self._stop.is_set():
                    break
                if method is None:
                    continue
                with self._lock:
                    self.items.append(json.loads(raw))
                channel.basic_ack(method.delivery_tag)
        finally:
            with contextlib.suppress(Exception):
                connection.close()

    def wait(self, count: int, timeout: float) -> list:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.items) >= count:
                    return list(self.items)
            time.sleep(0.05)
        raise AssertionError(f"expected {count} profiling results, got {len(self.items)}")
