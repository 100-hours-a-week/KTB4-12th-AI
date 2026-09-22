"""Install the separately shared, hash-pinned runtime and image archives locally."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
IMAGE_ARCHIVE_SHA256 = '7767a7f6f6c02cc0a03052a6336470281f06a12f5136eb383143202562f76530'
IMAGE_PREFIX = 'product-catalog-20260922-v1/'
DATA_FILES = {'data/catalog.json', 'data/manifest.json', 'data/vectors.f32'}

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()

def check_hash(path, expected):
    if sha(path) != expected:
        raise ValueError(f'Archive hash mismatch: {path.name}; no files installed')

def copy_entry(archive, entry, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix+'.part')
    try:
        with archive.open(entry) as source, temporary.open('wb') as out:
            shutil.copyfileobj(source, out)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

def install(runtime, images, root=ROOT):
    bundle = json.loads((root/'runtime-bundle.json').read_text())
    check_hash(runtime, bundle['sha256'])
    check_hash(images, IMAGE_ARCHIVE_SHA256)
    with zipfile.ZipFile(runtime) as data, zipfile.ZipFile(images) as pictures:
        if set(data.namelist()) != DATA_FILES or len(data.namelist()) != len(DATA_FILES):
            raise ValueError('Unexpected runtime archive entries')
        catalog = json.loads(data.read('data/catalog.json'))
        expected = {p[key].lstrip('/') for p in catalog['products'] for key in ('image','image_large','image_fallback')}
        if any(Path(p).parts[0] != 'images' or len(Path(p).parts) != 2 for p in expected):
            raise ValueError('Invalid image path')
        actual = {IMAGE_PREFIX + p for p in expected}
        if set(pictures.namelist()) != actual or len(pictures.namelist()) != len(actual):
            raise ValueError('Unexpected image archive entries')
        for entry in sorted(DATA_FILES):
            copy_entry(data, entry, root/entry)
        for entry in sorted(expected):
            copy_entry(pictures, IMAGE_PREFIX+entry, root/'static'/entry)
    (root/'runtime').mkdir(exist_ok=True)
    print(f"Installed {len(catalog['products'])} products and {len(expected)} image variants")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--images', required=True, type=Path)
    args = parser.parse_args()
    install(args.runtime, args.images)
