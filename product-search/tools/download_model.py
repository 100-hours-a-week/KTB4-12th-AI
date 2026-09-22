"""Download the pinned encoder from its publisher; verify all four file hashes."""
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def main():
    lock = json.loads((ROOT/'embedding/model.lock.json').read_text())
    folder = ROOT/'embedding/models'
    folder.mkdir(parents=True, exist_ok=True)
    for item in lock['files']:
        destination = folder/item['path']
        if destination.is_file() and sha(destination) == item['sha256']:
            print(f"verified {item['path']}", flush=True)
            continue
        url = f"https://huggingface.co/{lock['model']}/resolve/{lock['revision']}/{item['remotePath']}"
        temporary = destination.with_suffix(destination.suffix+'.part')
        print(f"downloading {item['path']} ({item['bytes']} bytes)", flush=True)
        try:
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open('wb') as out:
                for block in iter(lambda: response.read(1024 * 1024), b''):
                    out.write(block)
            if temporary.stat().st_size != item['bytes'] or sha(temporary) != item['sha256']:
                raise ValueError(f"Model hash mismatch: {item['path']}; original file was kept")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    print('Model files verified. No inference API credential is needed.')

if __name__ == '__main__':
    main()
