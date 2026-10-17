"""Catalog owner: publish one synthetic generation to a disposable DB only."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'product-search'))
from catalog_db import encode, projection, materialize


def publish_fixture(url, runtime_root, fixture):
    fixture = json.loads(Path(fixture).read_text())
    if fixture.get('synthetic') is not True:
        raise ValueError('This integration publisher accepts synthetic fixtures only')
    version_id = uuid.uuid4()
    package = fixture['generation'] + '-' + version_id.hex
    root = Path(runtime_root)
    with psycopg.connect(url, row_factory=dict_row) as db:
        # Product ingestion and active pointer are one owner transaction.
        category_ids = {c['sourceId']:c['id'] for c in fixture['categories']}
        for c in fixture['categories']:
            db.execute("""INSERT INTO ai_catalog.categories(id,parent_id,name,level,product_count,taxonomy_version)
                VALUES(%s,%s,%s,%s,0,%s) ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,parent_id=excluded.parent_id,level=excluded.level,taxonomy_version=excluded.taxonomy_version""",
                (c['id'],category_ids[c['parent']] if c['parent'] else None,c['name'],2 if c['parent'] else 1,package))
            db.execute("""INSERT INTO ai_catalog.integration_category_sources(category_id,source_category_id)
                VALUES(%s,%s) ON CONFLICT(category_id) DO UPDATE SET category_id=excluded.category_id""",(c['id'],c['sourceId']))
        for p in fixture['products']:
            db.execute('''INSERT INTO ai_catalog.products(source_product_id,id,name,brand,category_id,product_kind,product_type,description,unit_price,availability,view_count,source_provider,source_product_url,source_image_url,image_asset_id,display_image_path,package_id)
                VALUES(%s,%s,%s,'SYNTHETIC',%s,'합성','Shipping','합성 연결 검증 자료',%s,%s,%s,'SYNTHETIC',%s,%s,%s,%s,%s)
                ON CONFLICT(source_product_id) DO UPDATE SET id=excluded.id,name=excluded.name,category_id=excluded.category_id,view_count=excluded.view_count,unit_price=excluded.unit_price,availability=excluded.availability,source_product_url=excluded.source_product_url,source_image_url=excluded.source_image_url,image_asset_id=excluded.image_asset_id,display_image_path=excluded.display_image_path,package_id=excluded.package_id''',
                (p['sourceId'], p['id'], p['name'], category_ids[p['category']], p['price'], p['availability'], p['views'], p.get('productUrl',''), p.get('imageUrl',''), p.get('imageAssetId'), p.get('displayImagePath'), package))
        db.execute('''INSERT INTO ai_catalog.catalog_versions(id,package_id,taxonomy_version,product_count,category_count,source_sha256)
            VALUES(%s,%s,%s,%s,%s,%s)''', (version_id, package, package, len(fixture['products']), len(fixture['categories']), Jsonb({'fixture': hashlib.sha256(encode(fixture)).hexdigest()})))
        version = db.execute('SELECT * FROM ai_catalog.catalog_versions WHERE id=%s', (version_id,)).fetchone()
        catalog = projection(db, version)
        docs = root / 'synthetic-documents.json'
        vectors_path = root / 'synthetic-vectors.f32'
        docs.write_text(json.dumps([p['document'] for p in catalog['products']], ensure_ascii=False))
        subprocess.run(['node', str(root / 'embedding/documents.mjs'), str(docs), str(vectors_path)], check=True, timeout=180, capture_output=True)
        vectors = vectors_path.read_bytes()
        lock = json.loads((root / 'embedding/model.lock.json').read_text())
        manifest = {'model': lock['model'], 'dimensions': 768, 'precision': 'q4f16', 'max_tokens': 1024,
            'query_prefix': 'Query: ', 'document_prefix': 'Document: ',
            'model_sha256': next(f['sha256'] for f in lock['files'] if f['path'].endswith('onnx_data')),
            'catalog_sha256': hashlib.sha256(encode(catalog)).hexdigest(), 'vectors_sha256': hashlib.sha256(vectors).hexdigest(),
            'product_ids': [p['id'] for p in catalog['products']], 'source_product_ids': [p['source_product_id'] for p in catalog['products']],
            'token_counts': json.loads(Path(str(vectors_path) + '.json').read_text()), 'ai_catalog_version': str(version_id)}
        from search.encoder_contract import encoder_contract
        manifest['encoder_fingerprint'] = encoder_contract(root, manifest)
        db.execute('INSERT INTO ai_catalog.search_generations(catalog_version,manifest,vectors) VALUES(%s,%s,%s)', (version_id, Jsonb(manifest), vectors))
        db.execute('UPDATE ai_catalog.catalog_versions SET is_active=false WHERE is_active')
        db.execute('UPDATE ai_catalog.catalog_versions SET is_active=true WHERE id=%s', (version_id,))
    return materialize(url, root)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('runtime_root', type=Path)
    p.add_argument('--fixture', type=Path, default=ROOT / 'integration/tests/fixture.json')
    args = p.parse_args()
    import os
    print(json.dumps(publish_fixture(os.environ['CATALOG_OWNER_DATABASE_URL'], args.runtime_root, args.fixture)))
