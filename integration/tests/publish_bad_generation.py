"""Fault injection for a marked synthetic test DB only; never production."""
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'product-search'))
from catalog_db import encode, projection


def publish():
    with psycopg.connect(os.environ['CATALOG_OWNER_DATABASE_URL'], row_factory=dict_row) as db:
        if db.execute('SELECT current_user AS name').fetchone()['name'] != 'fixture_owner' or db.execute('SELECT count(*) AS n FROM ai_catalog.integration_marker').fetchone()['n'] != 1:
            raise ValueError('Fault injection requires the isolated marked fixture database')
        old = db.execute('SELECT * FROM ai_catalog.catalog_versions WHERE is_active').fetchone()
        if old['product_count'] != 3 or not old['package_id'].startswith('fixture-'):
            raise ValueError('Fault injection accepts only the three-product synthetic fixture')
        generation = db.execute('SELECT * FROM ai_catalog.search_generations WHERE catalog_version=%s', (old['id'],)).fetchone()
        new = dict(old, id=uuid.uuid4())
        db.execute('INSERT INTO ai_catalog.catalog_versions(id,package_id,taxonomy_version,product_count,category_count,source_sha256) VALUES(%s,%s,%s,%s,%s,%s)',
                   (new['id'],new['package_id'],new['taxonomy_version'],3,new['category_count'],Jsonb({'syntheticFaultInjection':True})))
        catalog = projection(db, new)
        manifest = dict(generation['manifest'], ai_catalog_version=str(new['id']),
                        catalog_sha256=hashlib.sha256(encode(catalog)).hexdigest(), encoder_fingerprint='synthetic-invalid-encoder')
        db.execute('INSERT INTO ai_catalog.search_generations(catalog_version,manifest,vectors) VALUES(%s,%s,%s)',
                   (new['id'],Jsonb(manifest),generation['vectors']))
        db.execute('UPDATE ai_catalog.catalog_versions SET is_active=false WHERE is_active')
        db.execute('UPDATE ai_catalog.catalog_versions SET is_active=true WHERE id=%s',(new['id'],))
    return {'badCatalogVersion':str(new['id']), 'restoreCatalogVersion':str(old['id'])}


if __name__ == '__main__':
    print(json.dumps(publish()))
