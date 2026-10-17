"""Backend-issued identities. Common mapping files also load in the profiler."""
import json
from pathlib import Path

BIGINT_MAX = 2**63 - 1


def product_id(value):
    if type(value) is not int or not 1 <= value <= BIGINT_MAX:
        raise ValueError('productId must be a positive signed BIGINT integer')
    return value


def load_id_mapping(path, kind='product'):
    path = Path(path)
    text = path.read_text()
    # Keep the previous local product bundle readable during migration.
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        document = None
    source_key = 'sourceProductId' if kind == 'product' else 'sourceCategoryId'
    target_key = 'backendProductId' if kind == 'product' else 'backendCategoryId'
    if isinstance(document, dict) and document.get('format') == 'product-search-id-mapping/1' and kind == 'product':
        rows, target_key = document['products'], 'productId'
    else:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        provenance = path.parent/'mapping-provenance.json'
        document = {'provenance': json.loads(provenance.read_text()) if provenance.exists() else {}}
    mapping, seen = {}, set()
    for row in rows:
        source, target = row[source_key], product_id(row[target_key])
        if not isinstance(source, str) or not source or source in mapping or target in seen:
            raise ValueError('Duplicate or invalid ID mapping')
        mapping[source] = target
        seen.add(target)
    if not mapping:
        raise ValueError('Empty ID mapping')
    return mapping, document
