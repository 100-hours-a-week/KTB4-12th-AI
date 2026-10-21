"""Bounded, expiring references to immutable catalog generations, on the API loop."""

import time
import uuid


class BindingUnavailable(Exception):
    pass


class CatalogBindings:
    LEASE_SECONDS = 600
    MAX_BINDINGS = 16
    MAX_GENERATIONS = 2

    def __init__(self, owner, *, clock=time.monotonic):
        self.owner = owner
        self.clock = clock
        self.entries = {}

    async def acquire(self, snapshot):
        await self.expire()
        service = self.owner.current
        if service.snapshot_id != snapshot:
            raise BindingUnavailable("SNAPSHOT_MISMATCH")
        generations = {entry[0] for entry in self.entries.values()} | {service}
        if (
            len(self.entries) >= self.MAX_BINDINGS
            or len(generations) > self.MAX_GENERATIONS
        ):
            raise BindingUnavailable("CATALOG_BINDING_CAPACITY")
        ident = uuid.uuid4().hex
        self.owner.readers[service] += 1
        self.entries[ident] = (service, self.clock() + self.LEASE_SECONDS)
        return ident, service

    def resolve(self, ident, snapshot=None):
        entry = self.entries.get(ident)
        if entry is None or entry[1] <= self.clock():
            raise BindingUnavailable("CATALOG_BINDING_EXPIRED")
        service = entry[0]
        if snapshot is not None and snapshot != service.snapshot_id:
            raise BindingUnavailable("SNAPSHOT_MISMATCH")
        self.entries[ident] = (service, self.clock() + self.LEASE_SECONDS)
        return service

    async def release(self, ident):
        entry = self.entries.pop(ident, None)
        if entry is not None:
            await self.owner._release_reader(entry[0])
        return entry is not None

    async def expire(self):
        expired = [
            ident for ident, (_, until) in self.entries.items() if until <= self.clock()
        ]
        for ident in expired:
            await self.release(ident)

    async def close(self):
        for ident in list(self.entries):
            await self.release(ident)
