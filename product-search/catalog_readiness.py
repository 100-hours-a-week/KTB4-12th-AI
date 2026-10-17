"""Database-mode readiness binds the loaded snapshot to the current DB UUID."""
import asyncio
import psycopg


def active_version(url):
    with psycopg.connect(url, connect_timeout=2) as db:
        row = db.execute('SELECT id FROM ai_catalog.catalog_versions WHERE is_active').fetchone()
    return str(row[0]) if row else None


async def matches_database(service, url, *, expected=None):
    active = await asyncio.to_thread(active_version, url)
    # Inspect after the DB read: a concurrently loaded snapshot must still match.
    loaded = service.current.catalog.get('ai_catalog_version')
    return bool(active and not service.load_error and loaded == active
                and (expected is None or active == expected))


async def synchronize(service, url, root, materialize):
    binding = await asyncio.to_thread(materialize, url, root)
    await service.refresh(retry_failed=True)
    if not await matches_database(service, url, expected=binding['catalogVersion']):
        raise RuntimeError('Loaded search generation does not match the active database catalog')
