"""Load only already-published local generations; never fetch Backend in the API."""
import asyncio
from collections import Counter
import json
import logging
from .encoder_contract import encoder_contract
from .service import SearchService
from .snapshots import active_data_dir
from .catalog_bindings import CatalogBindings

logger = logging.getLogger('product-search')


class ReloadingSearch:
    def __init__(self, root, service, embedding, *, encoder_fingerprint):
        self.root, self.current, self.embedding = root, service, embedding
        self.directory = service.data_dir
        self.encoder_fingerprint = encoder_fingerprint
        self.readers = Counter()
        self.retired = set()
        self.load_error = None
        self.failed_directory = None
        self.lock = asyncio.Lock()
        self.task = None
        self.stopping = asyncio.Event()
        self.bindings = CatalogBindings(self)

    def __getattr__(self, name):
        return getattr(self.current, name)

    def _load_candidate(self, directory):
        """Validate the published encoder before opening a replacement snapshot."""
        manifest = json.loads((directory / 'manifest.json').read_text())
        declared_fingerprint = manifest.get('encoder_fingerprint')
        if (declared_fingerprint != self.encoder_fingerprint
                or encoder_contract(self.root, manifest) != self.encoder_fingerprint):
            raise ValueError('Published encoder fingerprint differs from serving encoder')
        return SearchService(directory, self.embedding)

    async def _release_reader(self, service):
        """A replaced snapshot stays open until its last in-flight reader exits."""
        self.readers[service] -= 1
        if self.readers[service]:
            return
        del self.readers[service]
        if service in self.retired:
            self.retired.remove(service)
            await asyncio.to_thread(service.close)

    async def refresh(self, *, retry_failed=False):
        async with self.lock:
            directory = active_data_dir(self.root/'data')
            if directory == self.directory:
                self.load_error = None
                self.failed_directory = None
                return
            if directory == self.failed_directory and not retry_failed:
                return
            loading = asyncio.create_task(asyncio.to_thread(self._load_candidate, directory))
            try:
                candidate = await asyncio.shield(loading)
            except asyncio.CancelledError:
                async def discard_unpublished():
                    try:
                        unpublished = await loading
                    except Exception:
                        return
                    await asyncio.to_thread(unpublished.close)
                # A cancelled await cannot stop the loader thread. Retain and
                # close its eventual result without publishing a new snapshot.
                await asyncio.shield(asyncio.create_task(discard_unpublished()))
                raise
            except Exception:
                self.failed_directory = directory
                self.load_error = 'catalog_load_failed'
                raise
            previous_service, self.current = self.current, candidate
            self.directory = directory
            self.load_error = None
            self.failed_directory = None
            if self.readers[previous_service]:
                self.retired.add(previous_service)
            else:
                await asyncio.to_thread(previous_service.close)

    async def watch(self):
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.stopping.wait(),timeout=1)
                break
            except TimeoutError:
                pass
            try:
                await self.bindings.expire()
                await self.refresh()
            except Exception:
                self.load_error = 'catalog_load_failed'
                logger.exception('New catalog could not be loaded; serving previous snapshot')

    async def search(self, *args, **kwargs):
        service = self.current
        self.readers[service] += 1
        try:
            return await service.search(*args,**kwargs)
        finally:
            await self._release_reader(service)

    async def close(self):
        if self.task:
            self.stopping.set()
            await self.task
        await self.bindings.close()
        for service in [self.current,*self.retired]:
            await asyncio.to_thread(service.close)
