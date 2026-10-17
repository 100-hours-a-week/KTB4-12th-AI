"""Code-bound release ID; deployment preserves existing Space variables and secrets."""
import json,os
from pathlib import Path

def revision():
    path=Path(__file__).with_name('release.json')
    return json.loads(path.read_text())['revision'] if path.exists() else os.getenv('APP_REVISION','local')
