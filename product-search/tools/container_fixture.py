"""Generate three synthetic products with real model vectors for container CI.

Only writes to an empty, explicitly supplied directory. Never a serving catalog.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from search.encoder_contract import encoder_contract


def create(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError('Fixture destination must be empty')
    products = []
    for id, name, category_id, category, available in [
        (1, '검증 텀블러', 11, '음료', 'available'),
        (2, '검증 무선 스피커', 12, '전자', 'unknown'),
        (9223372036854775807, '검증 머그컵', 11, '음료', 'unavailable'),
    ]:
        products.append({
            'id': id, 'source_product_id': f'CI:{id}', 'name': name,
            'brand': '검증용', 'category_id': category_id,
            'source_category_id': f'CI:CAT:{category_id}', 'parent_category_id': 1,
            'category': category, 'category_group': '테스트', 'kind': '',
            'product_type': 'Shipping', 'price': 10000, 'description': name,
            'attributes': {}, 'tags': [], 'availability': available,
            'product_url': '', 'image': '', 'image_large': '', 'image_fallback': '',
            'document': name, 'description_origin': 'synthetic-ci',
        })
    catalog = {'format': 'product-search-catalog/3', 'source': 'synthetic-ci',
               'availability_note': 'CI fixture, not real products', 'products': products,
               'taxonomy': {'categories': [
                   {'category_id': id, 'source_category_id': f'CI:CAT:{id}',
                    'parent_id': parent, 'category': name, 'category_group': '테스트'}
                   for id, parent, name in [(1, None, '테스트'), (11, 1, '음료'), (12, 1, '전자')]
               ]}}
    raw = json.dumps(catalog, ensure_ascii=False).encode()
    (directory/'catalog.json').write_bytes(raw)
    documents = directory/'documents.json'
    documents.write_text(json.dumps([p['document'] for p in products], ensure_ascii=False))
    output = directory/'vectors.f32'
    subprocess.run(['node', str(ROOT/'embedding/documents.mjs'), str(documents), str(output)],
                   check=True, timeout=120)
    lock = json.loads((ROOT/'embedding/model.lock.json').read_text())
    manifest = {
        'model': lock['model'], 'dimensions': 768, 'precision': 'q4f16',
        'max_tokens': 1024, 'query_prefix': 'Query: ', 'document_prefix': 'Document: ',
        'model_sha256': next(f['sha256'] for f in lock['files'] if f['path'].endswith('onnx_data')),
        'catalog_sha256': hashlib.sha256(raw).hexdigest(),
        'vectors_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
        'product_ids': [p['id'] for p in products],
        'token_counts': json.loads(Path(str(output)+'.json').read_text()),
    }
    manifest['encoder_fingerprint'] = encoder_contract(ROOT, manifest)
    (directory/'manifest.json').write_text(json.dumps(manifest))
    documents.unlink()
    Path(str(output)+'.json').unlink()
    print('Created 3 synthetic CI products with real encoder vectors')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    create(parser.parse_args().directory.resolve())
