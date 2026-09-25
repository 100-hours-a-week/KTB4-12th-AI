"""Load only already-published local generations; never fetch Backend in the API."""
import asyncio
from collections import Counter
import json
import logging
from .encoder_contract import encoder_contract
from .service import SearchService
from .snapshots import active_data_dir

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

    def __getattr__(self, name):
        return getattr(self.current, name)

    async def refresh(self):
        async with self.lock:
            directory = active_data_dir(self.root/'data')
            if directory == self.directory or directory == self.failed_directory:
                return
            try:
                def load():
                    manifest = json.loads((directory/'manifest.json').read_text())
                    if manifest.get('encoder_fingerprint') != self.encoder_fingerprint or encoder_contract(self.root,manifest) != self.encoder_fingerprint:
                        raise ValueError('Published encoder fingerprint differs from serving encoder')
                    return SearchService(directory,self.embedding)
                candidate = await asyncio.to_thread(load)
            except Exception:
                self.failed_directory = directory
                self.load_error = 'catalog_load_failed'
                raise
            old, self.current = self.current, candidate
            self.directory = directory
            self.load_error = None
            self.failed_directory = None
            if self.readers[old]:
                self.retired.add(old)
            else:
                await asyncio.to_thread(old.close)

    async def watch(self):
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.stopping.wait(),timeout=1)
                break
            except TimeoutError:
                pass
            try:
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
            self.readers[service] -= 1
            if not self.readers[service]:
                del self.readers[service]
                if service in self.retired:
                    self.retired.remove(service)
                    await asyncio.to_thread(service.close)

    async def close(self):
        if self.task:
            self.stopping.set()
            await self.task
        for service in [self.current,*self.retired]:
            await asyncio.to_thread(service.close)
