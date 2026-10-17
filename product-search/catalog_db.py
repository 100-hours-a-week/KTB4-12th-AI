"""Read the authoritative catalog + vector generation in one DB snapshot.

Local files are disposable search caches, never an independent ingestion source.
DDL and activation belong to the catalog operator, not this service.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

import psycopg
from psycopg.rows import dict_row


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def validate_generation(catalog, manifest, vectors, version_id):
    """Compare one already-read DB generation without DB or filesystem access."""
    raw = encode(catalog)
    if (manifest.get('ai_catalog_version') != str(version_id) or
            hashlib.sha256(raw).hexdigest() != manifest['catalog_sha256'] or
            hashlib.sha256(vectors).hexdigest() != manifest['vectors_sha256'] or
            [p['id'] for p in catalog['products']] != manifest['product_ids'] or
            [p['source_product_id'] for p in catalog['products']] != manifest['source_product_ids'] or
            len(vectors) != len(catalog['products']) * manifest['dimensions'] * 4):
        raise ValueError('Canonical product/vector/version mismatch')
    return raw


def schema_version(db):
    return db.execute("SELECT EXISTS(SELECT FROM information_schema.columns WHERE table_schema='ai_catalog' AND table_name='products' AND column_name='id') AS modern").fetchone()['modern']


def projection(db, version):
    modern = schema_version(db)
    if modern:
        categories = db.execute("""SELECT c.*, c.id backend_category_id, m.source_category_id,
            p.id parent_id,p.name group_name FROM ai_catalog.categories c
            JOIN ai_catalog.integration_category_sources m ON m.category_id=c.id
            LEFT JOIN ai_catalog.categories p ON p.id=c.parent_id WHERE c.taxonomy_version=%s ORDER BY c.id""",(version['taxonomy_version'],)).fetchall()
    else:
        categories = db.execute('''SELECT c.*, p.backend_category_id parent_id, p.name group_name
        FROM ai_catalog.categories c LEFT JOIN ai_catalog.categories p
        ON p.source_category_id=c.parent_source_category_id
        WHERE c.taxonomy_version=%s ORDER BY c.backend_category_id''', (version['taxonomy_version'],)).fetchall()
    if len(categories) != version['category_count']:
        raise ValueError('Catalog category mapping/count differs from active version')
    taxonomy = []
    by_source = {}
    for c in categories:
        if c['backend_category_id'] is None:
            raise ValueError('Catalog category has no Backend ID mapping')
        row = {'category_id': c['backend_category_id'], 'source_category_id': c['source_category_id'],
               'parent_id': c['parent_id'], 'category': c['name'], 'category_group': c['group_name'] or c['name']}
        taxonomy.append(row)
        by_source[c['source_category_id']] = row
    if modern:
        rows = db.execute('''SELECT p.*,p.id backend_product_id,m.source_category_id FROM ai_catalog.products p
            JOIN ai_catalog.integration_category_sources m ON m.category_id=p.category_id
            WHERE p.package_id=%s ORDER BY p.id''',(version['package_id'],)).fetchall()
    else:
        rows = db.execute('SELECT * FROM ai_catalog.products WHERE package_id=%s ORDER BY backend_product_id',
                          (version['package_id'],)).fetchall()
    if len(rows) != version['product_count']:
        raise ValueError('Catalog product count differs from active version')
    products = []
    for p in rows:
        c = by_source[p['source_category_id']]
        if p['backend_product_id'] is None:
            raise ValueError('Catalog product has no Backend ID mapping')
        document = p['search_document'] or '\n'.join([f"상품명: {p['name']}", f"브랜드: {p['brand']}",
                              f"분류: {c['category']}", f"설명: {p['description']}"])
        products.append({'id': p['backend_product_id'], 'source_product_id': p['source_product_id'],
            'name': p['name'], 'normalized_name': p['normalized_name'] or p['name'], 'brand': p['brand'], 'category_id': c['category_id'],
            'source_category_id': c['source_category_id'], 'parent_category_id': c['parent_id'],
            'category': c['category'], 'category_group': c['category_group'],
            'kind': p['product_kind'], 'product_type': p['product_type'], 'price': p['unit_price'],
            'description': p['description'], 'attributes': p['attributes'], 'tags': [],
            'availability': p['availability'], 'product_url': p['source_product_url'],
            'image': p['display_image_path'] or '', 'image_large': p['display_image_large_path'] or '', 'image_fallback': p['display_image_fallback_path'] or '',
            'image_asset_id': p['image_asset_id'],
            'source_image_url': p['source_image_url'],
            'view_count': p['view_count'] or 0, 'source_updated_at': p['updated_at'].isoformat(),
            'document': document, 'description_origin': 'ai-catalog-db/1'})
    return {'format': 'product-search-catalog/3', 'source': 'ai-catalog-db/1',
            'ai_catalog_version': str(version['id']), 'availability_note': 'Canonical AI catalog; purchase state requires Backend confirmation',
            'taxonomy': {'categories': taxonomy}, 'products': products}


def materialize(url, root):
    root = Path(root)
    with psycopg.connect(url, row_factory=dict_row) as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        version = db.execute('SELECT * FROM ai_catalog.catalog_versions WHERE is_active').fetchone()
        if version is None:
            raise ValueError('No active canonical catalog')
        generation = db.execute('SELECT * FROM ai_catalog.search_generations WHERE catalog_version=%s',
                                (version['id'],)).fetchone()
        if generation is None:
            raise ValueError('Canonical catalog has no matching vector generation')
        catalog = projection(db, version)
    manifest, vectors = generation['manifest'], bytes(generation['vectors'])
    raw = validate_generation(catalog, manifest, vectors, version['id'])
    name = version['id'].hex
    target = root / 'data' / 'generations' / name
    payloads = {'catalog.json': raw, 'vectors.f32': vectors, 'manifest.json': encode(manifest)}
    target.mkdir(parents=True, exist_ok=True)
    for filename, data in payloads.items():
        path = target / filename
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError('Immutable derived generation differs; publish a new catalog version')
        else:
            temporary = target / (filename + '.' + uuid.uuid4().hex + '.tmp')
            temporary.write_bytes(data)
            os.replace(temporary, path)
    temporary = root / 'data' / ('CURRENT.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(name + '\n')
    os.replace(temporary, root / 'data' / 'CURRENT')
    return {'catalogVersion': str(version['id']), 'catalogSha256': manifest['catalog_sha256'],
            'vectorsSha256': manifest['vectors_sha256']}


class QAConnection:
    """Keep the small QA query interface while using the shared PostgreSQL."""
    def __init__(self, url):
        self.connection = psycopg.connect(url, options='-c search_path=ai_search,public')

    def execute(self, query, args=()):
        # All callers supply static SQL and bound values; no user SQL is accepted.
        query = query.replace('?', '%s').replace('LIMIT -1 OFFSET 5000', 'OFFSET 5000')
        return self.connection.execute(query, args)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.connection.rollback() if exc_type else self.connection.commit()
        finally:
            self.connection.close()
