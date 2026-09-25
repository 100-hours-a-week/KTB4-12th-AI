"""A version for observable search content and ranking, independent of fetch time."""
import hashlib
import json


def snapshot_id(catalog, manifest, algorithm):
    # Array order matters to browsing. Object insertion order and sync timestamps do not.
    products = [{key:value for key,value in product.items() if key != 'source_updated_at'}
                for product in catalog['products']]
    encoder_keys = ('model', 'dimensions', 'precision', 'max_tokens', 'query_prefix',
                    'document_prefix', 'model_sha256', 'encoder_fingerprint')
    payload = {
        'format': 'search-snapshot/1',
        'products': products,
        'taxonomy': catalog['taxonomy'],
        'source': catalog.get('source'),
        'availabilityNote': catalog.get('availability_note'),
        'documentContract': catalog.get('document_contract'),
        'vectors': manifest['vectors_sha256'],
        'encoder': {key:manifest.get(key) for key in encoder_keys},
        'tokenCounts': manifest.get('token_counts'),
        'algorithm': algorithm,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()[:20]
