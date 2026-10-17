"""Synthetic Docker initialization, after profiler migration, no paid account."""
import os
from pathlib import Path
import shutil
import sys

import psycopg

ROOT=Path('/workspace')
sys.path.insert(0,str(ROOT/'integration'))
from catalog import publish_fixture

url=os.environ['CATALOG_OWNER_DATABASE_URL']
with psycopg.connect(url) as db:
    db.execute((ROOT/'integration/schema.sql').read_text())
root=Path('/shared')
shutil.copytree('/app/embedding',root/'embedding',dirs_exist_ok=True)
shutil.copytree(ROOT/'integration/tests/media','/media',dirs_exist_ok=True)
publish_fixture(url,root,ROOT/'integration/tests/fixture.json')
