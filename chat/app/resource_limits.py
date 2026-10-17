"""Process resource guards, separate from business quotas and stored turn state."""
import os
from collections import Counter
from starlette.responses import StreamingResponse
from app.errors import APIError


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
        # Headroom for metadata/storage admission beside the existing four
        # generating turns. These are protective defaults, not throughput claims.
        self.pending_admissions = positive_setting('CHAT_MAX_PENDING_ADMISSIONS', 8)
        # Four views per turn allow a proxy plus tab/reconnect overlap. Total32
        # leaves headroom beyond four generating turns; no storage capacity claim.
        self.stream_total = positive_setting('CHAT_MAX_SSE_CONNECTIONS', 32)
        self.stream_per_turn = positive_setting('CHAT_MAX_SSE_PER_TURN', 4)
        self.retry_after = positive_setting('CHAT_RESOURCE_RETRY_AFTER_SECONDS', 2)
        self.streams = Counter()
        self.total_streams = 0

    def unavailable(self):
        return APIError(503, '채팅 요청이 많습니다. 같은 ID로 잠시 후 다시 확인해 주세요.',
                        headers={'Retry-After': str(self.retry_after)})

    def check_admission(self, admissions):
        if sum(not entry['accepted'].done() for entry in admissions.values()) >= self.pending_admissions:
            raise self.unavailable()

    def acquire_stream(self, runtime, tid):
        if self.total_streams >= self.stream_total or self.streams[tid] >= self.stream_per_turn:
            raise self.unavailable()
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
