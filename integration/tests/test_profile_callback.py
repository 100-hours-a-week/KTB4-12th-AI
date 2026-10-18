"""Deletion knowledge must also apply to a previously persisted profile callback."""
from uuid import uuid4

import httpx
import pytest
from profiling.intake import resend_callback
from profiling.types import CallbackResult, ProfileOutcome, RunStatus, SearchResult

from integration.profile_callback import VisibleProductsBackend


class Backend:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send_profile_callback(self, outcome):
        self.sent.append(outcome.model_dump())
        return CallbackResult(status=RunStatus.DELIVERED, http_status=200)

    def close(self):
        self.closed = True


def outcome():
    return ProfileOutcome(recipient_user_id=55, source_version=3, status=RunStatus.DELIVERED,
                          callback_attempts=2, search=SearchResult(product_ids=[101, 102],
                          query_text='합성', catalog_version_id=uuid4()))


@pytest.mark.parametrize('status,body', [
    (200, {'products': [{'productId': 102}], 'missingIds': [101]}),
    (200, {'products': [], 'missingIds': []}),
    (401, {}), (503, {}), (200, {'malformed': True}),
])
def test_stored_resend_does_not_send_or_rewrite_if_current_visibility_unconfirmed(status, body):
    backend = Backend()
    client = VisibleProductsBackend(backend, 'http://search', 'synthetic',
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)))
    class Store:
        def save(self, _):
            pytest.fail('Blocked callback must preserve the durable payload and counters')
    saved = outcome()
    before = saved.model_dump()
    try:
        resend_callback(saved, client, Store())
        assert not backend.sent and saved.model_dump() == before
    finally:
        client.close()


def test_deletion_between_network_retries_retains_count_of_actual_send():
    backend = Backend()
    saved = outcome()
    writes, checks = [], []
    class Store:
        def save(self, value):
            writes.append(value)
    def handler(_):
        checks.append(True)
        return httpx.Response(200, json={'products': [{'productId': 101}, {'productId': 102}],
                                        'missingIds': []} if len(checks) == 1 else
                                       {'products': [{'productId': 102}], 'missingIds': [101]})
    def send(value):
        backend.sent.append(value.model_dump())
        return CallbackResult(status=RunStatus.RESULT_READY, http_status=503)
    backend.send_profile_callback = send
    client = VisibleProductsBackend(backend, 'http://search', 'synthetic', transport=httpx.MockTransport(handler))
    try:
        resend_callback(saved, client, Store(), sleep=lambda _: None)
        assert len(backend.sent) == 1 and len(checks) == 2
        assert writes[0].search == saved.search and writes[0].callback_attempts == 3
        assert writes[0].status == RunStatus.RESULT_READY
    finally:
        client.close()
    assert backend.closed


def test_visible_resend_preserves_exact_ids_and_counts_only_sent_attempt():
    backend = Backend()
    saved = outcome()
    writes = []
    class Store:
        def save(self, value):
            writes.append(value)
    def handler(request):
        assert request.url.path == '/v1/products'
        assert request.headers['Authorization'] == 'Bearer synthetic'
        return httpx.Response(200, json={'products': [{'productId': 102}, {'productId': 101}], 'missingIds': []})
    client = VisibleProductsBackend(backend, 'http://search', 'synthetic', transport=httpx.MockTransport(handler))
    try:
        resend_callback(saved, client, Store())
        assert backend.sent == [saved.model_dump()]
        assert writes[0].search == saved.search and writes[0].callback_attempts == 3
    finally:
        client.close()
