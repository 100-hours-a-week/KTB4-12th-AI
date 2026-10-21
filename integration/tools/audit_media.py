"""Read-only full media inventory/decoding; no network or source modification."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil

from PIL import Image


def urls(value):
    if isinstance(value, str):
        return [value] if value.startswith(('https://', 'http://', '//')) else []
    if isinstance(value, list):
        return [u for item in value for u in urls(item)]
    if isinstance(value, dict):
        return [u for item in value.values() for u in urls(item)]
    return []


def normalized(url):
    return 'https:' + url if url.startswith('//') else url


def check_file(item):
    path, expected = item
    out = {'path': str(path), 'exists': path.is_file()}
    if not out['exists']:
        return out
    data = path.read_bytes()
    out.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    out['hashMatches'] = not expected or out['sha256'] == expected
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            out.update(decoded=True, format=image.format, width=image.width, height=image.height)
    except Exception as exc:
        out.update(decoded=False, errorClass=type(exc).__name__)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--catalog', type=Path, required=True)
    p.add_argument('--package', type=Path, required=True)
    p.add_argument('--image-root', type=Path, required=True)
    p.add_argument('--original-export', type=Path, required=True)
    p.add_argument('--detail-inventory', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--include-detail', action='store_true', help='Optional inventory only; detail images are excluded by default')
    args = p.parse_args()
    catalog = json.loads(args.catalog.read_text())
    packaged = [json.loads(line) for line in (args.package/'data/products.jsonl').read_text().splitlines() if line.strip()]
    assets = json.loads((args.package/'data/image-assets.json').read_text())['assets']
    originals = json.loads((args.original_export/'image-manifest.json').read_text())
    details = json.loads(args.detail_inventory.read_text())
    records, checks, known = [], {}, {}
    for item in originals:
        path = args.original_export/item['image_path']
        if not path.is_file() and Path(item['image_path']).is_absolute():
            path = Path(item['image_path'])
        checks[path] = item['sha256']
        known[normalized(item['source_url'])] = {'path': str(path), 'sha256': item['sha256']}
    derivatives = []
    for asset in assets:
        for variant in asset['variants'].values():
            path = args.image_root/Path(variant['path']).name
            checks[path] = variant['sha256']
            derivatives.append(str(path))
    sets = {'representative': set(), 'additional': set(), 'detail': set()}
    appearances = Counter()
    for row in details:
        raw = json.loads(Path(row['raw_path']).read_text())
        images = {'representative': urls(raw.get('imageUrl')), 'additional': urls(raw.get('optionalImageUrls')),
                  'detail': [image['src'] for image in row['images'] if image.get('src')] if args.include_detail else []}
        references = []
        for kind, entries in images.items():
            for order, source in enumerate(entries):
                source = normalized(source)
                sets[kind].add(source); appearances[kind] += 1
                references.append({'kind': kind, 'order': order, 'sourceUrl': source, 'existingOriginal': known.get(source)})
        records.append({'sourceProductId': 'KAKAO_GIFT:'+str(row['id']), 'images': references})
    files = list(ThreadPoolExecutor(max_workers=2).map(check_file, checks.items()))
    by_path = {item['path']: item for item in files}
    valid_urls = {url for url, item in known.items() if by_path[item['path']].get('decoded') and by_path[item['path']].get('hashMatches')}
    required = set().union(*sets.values())
    source_ids = {p['sourceProductId'] for p in packaged}
    normalized_ids = {p['source_product_id'] for p in catalog['products']}
    summary = {'catalogProducts':len(catalog['products']), 'sourcePackageProducts':len(packaged),
        'sourceIdsMatch':source_ids==normalized_ids, 'originalImageManifestRows':len(originals),
        'originalUniqueByteHashes':len({p['sha256'] for p in originals}), 'derivativeFilesExpected':len(derivatives),
        'localFilesInspected':len(files), 'fileExists':sum(i['exists'] for i in files),
        'decodedAndHashValid':sum(bool(i.get('decoded') and i.get('hashMatches')) for i in files),
        'missingFiles':sum(not i['exists'] for i in files), 'decodeOrHashFailures':sum(i['exists'] and not(i.get('decoded') and i.get('hashMatches')) for i in files),
        'localBytes':sum(i.get('bytes',0) for i in files), 'requiredUniqueImageUrlsAllKinds':len(required),
        'requiredRoles':['representative','additional'] + (['detail'] if args.include_detail else []),
        'missingUrlCountIsContentUniqueFileCount':False,
        'requiredOriginalUrlsPresentAndDecoded':len(required & valid_urls), 'missingUniqueUrls':len(required-valid_urls),
        'roles':{kind:{'occurrences':appearances[kind], 'uniqueUrls':len(values), 'existingDecodedOriginals':len(values&valid_urls), 'missingUrls':len(values-valid_urls)} for kind,values in sets.items()},
        'diskFreeBytes':shutil.disk_usage(args.output.parent).free,
        'allImagesComplete':not(required-valid_urls) and all(i.get('decoded') and i.get('hashMatches') for i in files),
        'networkDownloads':0, 'sourceFilesModified':False}
    args.output.write_text(json.dumps({'summary':summary, 'files':files, 'products':records, 'catalogMappingProvenance':catalog.get('id_mapping_provenance')},ensure_ascii=False)+'\n')
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
