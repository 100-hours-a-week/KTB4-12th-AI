"""Code-bound release ID; deployment preserves existing Space variables and secrets."""
import json,os
from pathlib import Path

_PATH=Path(__file__).with_name('release.json')
_REVISION=json.loads(_PATH.read_text())['revision'] if _PATH.exists() else os.getenv('APP_REVISION','local')

def revision():
    return _REVISION
