"""Fault injection: deployment checks must reject plausible broken responses."""
import io
import json
from urllib.parse import urlsplit

import pytest

from tools.smoke_search import smoke


def install_http_stub(monkeypatch, *, fixture=False, fault=None):
    ids = [1, 2, 9223372036854775807] if fixture else [7, 8, 9]
    hits = [{'productId': id, 'availability': availability, 'denseScore': .5}
            for id, availability in zip(ids, ['available', 'unknown', 'unavailable'])]

    def respond(request, timeout):
        path = urlsplit(request.full_url).path
        payload = json.loads(request.data) if request.data else {}
        status = 200
        if path == '/readyz':
            body = {'products': 3, 'snapshotId': 'test', 'catalogLoadError': None}
        elif path == '/v1/metadata':
            body = {'source': 'synthetic-ci' if fixture else 'real-catalog',
                    'productCount': 4 if fault == 'metadata' else 3, 'snapshotId': 'test'}
        elif path == '/v1/products':
            body = {'products': [h for id in payload['ids'] for h in hits if h['productId'] == id], 'missingIds': []}
            if fault == 'details':
                body['products'] = []
        elif path == '/v1/search':
            if payload.get('limit') == 0:
                status, body = 422, {'error': {'code': 'INVALID_REQUEST'}}
            elif payload.get('snapshotId') == 'outdated':
                status, body = 409, {'error': {'code': 'SNAPSHOT_MISMATCH'}}
            else:
                selected = hits[:2] if 'filters' in payload else hits
                if fault == 'filter' and 'filters' in payload:
                    selected = []
                start, limit = payload.get('offset', 0), payload.get('limit', 30)
                body = {'hits': selected[start:start+limit], 'snapshotId': 'test',
                        'nextOffset': start+limit if start+limit < len(selected) else None}
                if fault == 'dense' and payload.get('mode') == 'dense':
                    body['hits'] = [dict(h, denseScore=None) for h in body['hits']]
        elif path in ('/', '/guide'):
            body = b'<html><link rel="stylesheet" href="/assets/app.css"><script src="/assets/app.js"></script></html>'
        elif path.startswith('/assets/'):
            status, body = (404, b'missing') if fault == 'asset' else (200, b'/* asset */')
        else:
            raise AssertionError(f'Unexpected request: {path}')
        response = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
        response.status = status
        return response

    monkeypatch.setattr('urllib.request.urlopen', respond)


def test_three_real_products_do_not_imply_ci_product_ids(monkeypatch):
    install_http_stub(monkeypatch)
    smoke('http://test', expected_products=3)


def test_explicit_ci_fixture_contract(monkeypatch):
    install_http_stub(monkeypatch, fixture=True)
    smoke('http://test', expected_products=3, fixture=True)


@pytest.mark.parametrize('fault,message', [
    ('metadata', 'Metadata product count'),
    ('details', 'Product details must match'),
    ('filter', 'Availability filter dropped'),
    ('dense', 'Dense scores must be finite'),
    ('asset', '/assets/app.css'),
])
def test_broken_deployment_cannot_pass_smoke(monkeypatch, fault, message):
    install_http_stub(monkeypatch, fixture=True, fault=fault)
    with pytest.raises(AssertionError, match=message):
        smoke('http://test', expected_products=3, fixture=True)
