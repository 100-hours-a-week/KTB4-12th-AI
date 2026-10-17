"""Immutable generations and a single atomic pointer; one local sync writer."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re


def active_data_dir(data):
    data = Path(data)
    pointer = data/'CURRENT'
    if not pointer.exists():
        return data
    name = pointer.read_text().strip()
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
        raise ValueError('Invalid generation pointer')
    path = data/'generations'/name
    if not path.is_dir():
        raise ValueError('Missing generation')
    return path


@contextmanager
def sync_lock(data):
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    with (data/'.sync.lock').open('a') as lock:
        # Acquired BEFORE HTTP fetch. Fail fast instead of piling up stale jobs.
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another catalog sync is running') from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def write_bytes(path, data):
    with Path(path).open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def publish(data, generation):
    pointer = Path(data)/'CURRENT.tmp'
    write_bytes(pointer, (generation+'\n').encode())
    os.replace(pointer, Path(data)/'CURRENT')
    fd = os.open(data, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
