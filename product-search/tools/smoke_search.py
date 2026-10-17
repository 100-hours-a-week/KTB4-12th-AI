"""HTTP startup/inference/contract smoke. QA writes require --qa-receipt."""
import argparse
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import time
import urllib.error
import urllib.request


class LocalAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        path = attrs.get('src') if tag == 'script' else attrs.get('href') if tag == 'link' and attrs.get('rel') == 'stylesheet' else None
        if path and path.startswith('/') and not path.startswith('//'):
            self.paths.add(path)


def smoke(url, *, expected_products=None, fixture=False, qa_receipt=None, resume=None):
    def call(path, payload=None, expected=200):
        request = urllib.request.Request(url.rstrip('/')+path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Content-Type': 'application/json', 'X-Search-Source': 'chat'})
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status, body = response.status, response.read()
        assert status == expected, (path, status, expected)
        return json.loads(body) if body.startswith(b'{') else body

    deadline = time.monotonic()+120
    while True:
        try:
            ready = call('/readyz')
            break
        except (OSError, AssertionError):
            if time.monotonic() >= deadline:
                raise RuntimeError('Search readiness timeout')
            time.sleep(.5)
    if expected_products is not None:
        assert ready['products'] == expected_products, ready
    assert ready['catalogLoadError'] is None
    metadata = call('/v1/metadata')
    assert metadata['snapshotId'] == ready['snapshotId'], 'Metadata snapshot differs from readiness'
    assert metadata['productCount'] == ready['products'], 'Metadata product count differs from readiness'
    if fixture:
        assert metadata['source'] == 'synthetic-ci' and ready['products'] == 3, 'Expected the synthetic CI catalog'
    hybrid = call('/v1/search', {'query': '검증 텀블러', 'limit': 3})
    assert hybrid['hits'] and hybrid['snapshotId'] == ready['snapshotId']
    assert all(type(hit['productId']) is int for hit in hybrid['hits'])
    dense = call('/v1/search', {'query': '음악', 'mode': 'dense', 'limit': 3})
    assert dense['hits'] and all(hit['denseScore'] is not None and math.isfinite(hit['denseScore']) for hit in dense['hits']), 'Dense scores must be finite'
    requested_ids = [h['productId'] for h in hybrid['hits']]
    details = call('/v1/products', {'ids': requested_ids,
                                  'snapshotId': hybrid['snapshotId']})
    assert not details['missingIds']
    assert [p['productId'] for p in details['products']] == requested_ids, 'Product details must match requested IDs'
    filtered = call('/v1/search', {'filters': {'availability': 'available_or_unknown'}, 'limit': 100})
    assert all(h['availability'] != 'unavailable' for h in filtered['hits'])
    if fixture:
        assert {h['productId'] for h in filtered['hits']} == {1, 2}, 'Availability filter dropped eligible CI products'
        lexical = call('/v1/search', {'query': '검증 텀블러', 'mode': 'lexical', 'limit': 1})
        assert lexical['hits'][0]['productId'] == 1, 'Expected the exact CI lexical match'
    first = call('/v1/search', {'limit': 1})
    if first['nextOffset'] is not None:
        second = call('/v1/search', {'limit': 1, 'offset': first['nextOffset'],
                                    'snapshotId': first['snapshotId']})
        assert first['hits'][0]['productId'] != second['hits'][0]['productId']
    assert call('/v1/search', {'limit': 0}, 422)['error']['code'] == 'INVALID_REQUEST'
    assert call('/v1/search', {'snapshotId': 'outdated'}, 409)['error']['code'] == 'SNAPSHOT_MISMATCH'
    for path in ('/', '/guide'):
        html = call(path)
        assert b'<html' in html
        assets = LocalAssets()
        assets.feed(html.decode())
        assert assets.paths, 'Expected local web assets'
        for asset in sorted(assets.paths):
            assert call(asset), f'Empty web asset: {asset}'
    if fixture:
        maximum = 9223372036854775807
        assert call('/v1/products', {'ids': [maximum]})['products'][0]['productId'] == maximum
    if qa_receipt:
        record = call('/api/search', {'query': '검증 텀블러', 'limit': 1})
        Path(qa_receipt).write_text(json.dumps({'searchId': record['searchId']}))
    if resume:
        id = json.loads(Path(resume).read_text())['searchId']
        assert call('/api/searches/'+id)['searchId'] == id
    print(json.dumps({'status': 'passed', 'products': ready['products'],
                      'snapshotId': ready['snapshotId'], 'qaPersistenceChecked': bool(resume)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--expected-products', type=int)
    parser.add_argument('--fixture', action='store_true', help='Validate tools/container_fixture.py product identities and expected results')
    parser.add_argument('--qa-receipt', type=Path, help='Create one QA record, save its ID here')
    parser.add_argument('--resume', type=Path, help='Verify a previously saved QA record survived restart')
    args = parser.parse_args()
    smoke(args.url, expected_products=args.expected_products, fixture=args.fixture, qa_receipt=args.qa_receipt, resume=args.resume)
