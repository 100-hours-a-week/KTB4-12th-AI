"""Cheap readiness reads backed by periodic uncached end-to-end probes."""
import asyncio
from contextlib import suppress
from .models import SearchRequest


class SearchHealth:
    def __init__(self, service, interval=30):
        self.service = service
        self.interval = interval
        self.failed = True
        self.lock = asyncio.Lock()
        self.task = None

    @property
    def ready(self):
        return not self.failed and self.service.embedding.ready

    async def check(self):
        async with self.lock:
            try:
                await self.service.search(SearchRequest(query='검색 상태 확인', limit=1),
                                          source='profile', use_cache=False)
                self.failed = False
            except Exception:
                self.failed = True
        return self.ready

    async def watch(self):
        while True:
            await asyncio.sleep(self.interval)
            # Avoid competing with user searches. Dead workers are not probed:
            # their queue cannot drain and readiness must remain false.
            embedding = self.service.embedding
            if embedding.task is None or embedding.task.done():
                self.failed = True
                continue
            if not embedding.pending:
                await self.check()

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
