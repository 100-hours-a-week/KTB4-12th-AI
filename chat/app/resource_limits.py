"""Process resource guards, separate from business quotas and stored turn state."""
import os
import asyncio
from contextvars import ContextVar
from collections import Counter
from starlette.responses import StreamingResponse
from app.errors import APIError


# Only synchronous storage dispatch reads this context; it never grants admission.
PREFLIGHT_STORAGE = ContextVar("chat_preflight_storage", default=False)

def positive_setting(name, default):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(name + ' must be a positive integer') from None
    if value <= 0:
        raise ValueError(name + ' must be a positive integer')
    return value


class ResourceLimits:
    def __init__(self):
        # Storage admission protection is independent of generation counts.
        self.pending_admissions = positive_setting('CHAT_MAX_PENDING_ADMISSIONS', 8)
        self.preflight_limit = positive_setting('CHAT_MAX_PREFLIGHT_READS', 8)
        self.preflight_reads = 0
        self.retry_after = positive_setting('CHAT_RESOURCE_RETRY_AFTER_SECONDS', 2)
        self.streams = Counter()
        self.total_streams = 0

    def unavailable(self):
        return APIError(503, '채팅 요청이 많습니다. 같은 ID로 잠시 후 다시 확인해 주세요.',
                        headers={'Retry-After': str(self.retry_after)})

    async def preflight(self, store, method, **args):
        """Fail before dispatch, retaining capacity until the owned read finishes.

        Separate from pending admission: a terminal replay still works when new
        generation is full. No request task or executor wait queue is unbounded.
        """
        if self.preflight_reads >= self.preflight_limit:
            raise self.unavailable()
        self.preflight_reads += 1
        async def read():
            token = PREFLIGHT_STORAGE.set(True)
            try:
                return await store.call(method, **args)
            finally:
                PREFLIGHT_STORAGE.reset(token)
                self.preflight_reads -= 1
        task = asyncio.create_task(read())
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.shield(task)

    def check_admission(self, admissions):
        if sum(not entry['accepted'].done() for entry in admissions.values()) >= self.pending_admissions:
            raise self.unavailable()

    def acquire_stream(self, runtime, tid):
        # Track lifecycle for cleanup without rejecting by connection count.
        self.total_streams += 1
        self.streams[tid] += 1
        return StreamLease(self, runtime, tid)


def limits_for(runtime):
    limits = getattr(runtime, 'resource_limits', None)
    if limits is None:
        limits = runtime.resource_limits = ResourceLimits()
    return limits


def notify_turn(runtime, tid):
    signal = runtime.signals.get(tid)
    if signal is not None:
        signal.set()


def cleanup_turn(runtime, tid):
    if (tid not in getattr(runtime, 'jobs', {}) and tid not in getattr(runtime, 'admissions', {})
            and not limits_for(runtime).streams.get(tid, 0)):
        runtime.signals.pop(tid, None)
        getattr(runtime, 'cache', {}).pop(tid, None)


class StreamLease:
    def __init__(self, limits, runtime, tid):
        self.limits, self.runtime, self.tid = limits, runtime, tid
        self.released = False

    def release(self):
        if self.released:
            return
        self.released = True
        self.limits.total_streams -= 1
        self.limits.streams[self.tid] -= 1
        if not self.limits.streams[self.tid]:
            del self.limits.streams[self.tid]
        cleanup_turn(self.runtime, self.tid)


class ManagedStreamingResponse(StreamingResponse):
    def __init__(self, content, lease, **kwargs):
        self.lease = lease
        super().__init__(content, **kwargs)

    async def __call__(self, scope, receive, send):
        # Covers disconnect/cancellation even before the body iterator starts.
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.body_iterator.aclose()
            finally:
                self.lease.release()
