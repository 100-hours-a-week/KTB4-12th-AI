"""Code-bound release ID; deployment preserves existing Space variables and secrets."""
import json,os
from pathlib import Path

_PATH=Path(__file__).with_name('release.json')
_REVISION=json.loads(_PATH.read_text())['revision'] if _PATH.exists() else os.getenv('APP_REVISION','local')

def revision():
    return _REVISION


def build_commit():
    """Read build-time provenance, never infer Git HEAD from a prompt fingerprint."""
    import re
    path = Path(__file__).with_name('build-revision.txt')
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value if re.fullmatch(r'[0-9a-f]{40}', value) else None
