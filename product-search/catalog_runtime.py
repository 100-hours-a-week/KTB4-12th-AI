"""One serving Search validates pending PG generations and switches the pointer.

Only payload preparation runs in the sync job. Index loading/encoder validation
and the active-pointer commit belong to this process; restart revalidates PG.
"""
import asyncio
from contextlib import asynccontextmanager
import logging

from catalog_store import CatalogStore, SERVER_LOCK, materialize_generation
from search.bootstrap import open_search
from search.product_tags import validate as validate_tags
from search.snapshots import publish

log = logging.getLogger("catalog-runtime")


class CatalogRuntime:
    def __init__(self, store, root, owner):
        self.store, self.root, self.owner = store, root, owner
        self.connection = None
        self.ready = False
        self.transition = False

    def claim(self):
        if self.connection is not None:
            try:
                self.connection.execute("SELECT 1")
                return
            except Exception:
                self.connection.close()
                self.connection = None
        connection = self.store.connect(autocommit=True)
        try:
            if not connection.execute("SELECT pg_try_advisory_lock(%s) AS held", (SERVER_LOCK,)).fetchone()["held"]:
                raise RuntimeError("ONLY_ONE_SEARCH_PROCESS_SUPPORTED")
        except BaseException:
            connection.close()
            raise
        self.connection = connection

    def load(self, ident):
        generation = self.store.load(ident)
        validate_tags(generation.catalog)
        directory = materialize_generation(generation, self.root)
        candidate = self.owner._load_candidate(directory)
        if candidate.manifest.get("model_revision") != self.owner.current.manifest.get("model_revision"):
            candidate.close()
            raise ValueError("ENCODER_REVISION_CHANGE_REQUIRES_RESTART")
        return candidate

    async def swap(self, candidate):
        publish(self.root / "data", candidate.data_dir.name)
        old = self.owner.current
        self.owner.current = candidate
        self.owner.directory = candidate.data_dir
        self.owner.load_error = self.owner.failed_directory = None
        if self.owner.readers[old]:
            self.owner.retired.add(old)
        else:
            await asyncio.to_thread(old.close)

    async def poll(self):
        await asyncio.to_thread(self.claim)
        state = await asyncio.to_thread(self.store.state)
        loaded = self.owner.current.catalog["ai_catalog_version"]
        # Reconcile an uncertain commit or crash before accepting any new call.
        if state["active_id"] and state["active_id"] != loaded:
            self.ready = False
            candidate = await asyncio.to_thread(self.load, state["active_id"])
            await self.swap(candidate)
        pending = state["pending_id"]
        if pending:
            try:
                candidate = await asyncio.to_thread(self.load, pending)
            except Exception:
                await asyncio.to_thread(self.store.reject, pending, self.connection)
                log.error("Pending catalog rejected; previous generation retained")
            else:
                self.transition = True
                self.ready = False
                try:
                    await asyncio.to_thread(self.store.activate, pending, state["active_id"], self.connection)
                    await self.swap(candidate)
                    candidate = None
                finally:
                    self.transition = False
                    if candidate is not None and candidate is not self.owner.current:
                        await asyncio.to_thread(candidate.close)
        state = await asyncio.to_thread(self.store.state)
        self.ready = state["active_id"] == self.owner.current.catalog["ai_catalog_version"]

    async def check(self):
        if not self.ready or self.transition:
            return False
        try:
            # A broken lock connection also closes readiness after PG restarts.
            await asyncio.to_thread(self.connection.execute, "SELECT 1")
            state = await asyncio.to_thread(self.store.state)
            return state["active_id"] == self.owner.current.catalog["ai_catalog_version"]
        except Exception:
            self.ready = False
            return False


@asynccontextmanager
async def open_database_search(root, url):
    store = CatalogStore(url)
    state = await asyncio.to_thread(store.state)
    ident = state["active_id"] or state["pending_id"]
    if not ident:
        raise RuntimeError("CATALOG_SYNC_REQUIRED")
    generation = await asyncio.to_thread(store.load, ident)
    validate_tags(generation.catalog)
    directory = await asyncio.to_thread(materialize_generation, generation, root)
    publish(root / "data", directory.name)
    async with open_search(root, watch=False) as owner:
        runtime = CatalogRuntime(store, root, owner)
        owner.database_runtime = runtime
        stopping = asyncio.Event()
        task = None
        try:
            await runtime.poll()
            if not runtime.ready:
                raise RuntimeError("CATALOG_NOT_ACTIVE")
            async def watch():
                while not stopping.is_set():
                    try:
                        await asyncio.wait_for(stopping.wait(), timeout=1)
                    except TimeoutError:
                        try:
                            await owner.bindings.expire()
                            await runtime.poll()
                        except Exception:
                            runtime.ready = False
                            log.error("Catalog PG connection or activation unavailable")
            task = asyncio.create_task(watch())
            yield owner
        finally:
            stopping.set()
            if task:
                await task
            if runtime.connection is not None:
                await asyncio.to_thread(runtime.connection.close)
