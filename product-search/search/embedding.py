"""One managed encoder, bounded scheduling, shared LRU and single-flight queries."""
import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import heapq
import itertools
import json
import os
from pathlib import Path
import select
import subprocess
import threading
import time
import numpy as np


class SearchBusy(Exception):
    pass


class Encoder:
    def __init__(self, directory: Path, verify=None):
        self.directory = directory
        self.verify = verify
        self.process = None
        self._process_lock = threading.Lock()
        self._stopping = threading.Event()
        self._buffer = bytearray()

    def start(self):
        try:
            if self.verify is not None:
                self.verify()
            with self._process_lock:
                if self._stopping.is_set():
                    raise RuntimeError("임베딩 실행기가 종료 중입니다.")
                self.process = subprocess.Popen(["node", "service.mjs"], cwd=self.directory,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
                os.set_blocking(self.process.stdout.fileno(), False)
                self._buffer.clear()
            if self._read_response(60, "임베딩 모델 준비 시간 초과").get("ready") is not True:
                raise RuntimeError("임베딩 모델 준비 실패")
        except (OSError, ValueError, TypeError) as exc:
            self.close()
            raise RuntimeError("임베딩 모델 준비 실패") from exc
        except RuntimeError:
            self.close()
            raise

    def _read_response(self, timeout, timeout_message):
        deadline = time.monotonic() + timeout
        process = self.process
        if process is None:
            raise RuntimeError("임베딩 실행기가 종료되었습니다.")
        descriptor = process.stdout.fileno()
        while b'\n' not in self._buffer:
            if self._stopping.is_set():
                raise RuntimeError("임베딩 실행기가 종료 중입니다.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(timeout_message)
            if not select.select([descriptor], [], [], min(remaining, 0.1))[0]:
                continue
            try:
                chunk = os.read(descriptor, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                raise RuntimeError("임베딩 실행기가 응답 전에 종료되었습니다.")
            self._buffer.extend(chunk)
            if len(self._buffer) > 1024 * 1024:
                raise RuntimeError("임베딩 응답 크기가 제한을 초과했습니다.")
        line, _, remainder = self._buffer.partition(b'\n')
        self._buffer = bytearray(remainder)
        response = json.loads(line)
        if not isinstance(response, dict):
            raise RuntimeError("임베딩 응답이 올바르지 않습니다.")
        return response

    def encode(self, query):
        try:
            process = self.process
            if process is None or process.poll() is not None:
                self.close()
                self.start()
                process = self.process
            if process is None:
                raise RuntimeError("임베딩 실행기가 종료되었습니다.")
            process.stdin.write(json.dumps({"text": query}, ensure_ascii=False) + "\n")
            process.stdin.flush()
            response = self._read_response(15, "임베딩 응답 시간 초과")
            vector = np.asarray(response.get("vector", []), dtype=np.float32)
            if vector.shape != (768,) or not np.isfinite(vector).all() or np.linalg.norm(vector) == 0:
                raise RuntimeError("임베딩 응답이 올바르지 않습니다.")
            return vector / np.linalg.norm(vector)
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            # Transport/protocol failures are server failures, never invalid search input.
            self.close()
            raise RuntimeError("임베딩 실행기 통신 또는 응답 오류") from exc
        except RuntimeError:
            self.close()
            raise

    def close(self):
        with self._process_lock:
            process, self.process = self.process, None
        if process:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    process.kill()
                process.wait(timeout=1)
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        with suppress(OSError):
                            stream.close()

    def shutdown(self):
        # Permanent stop also covers cancellation while verify()/Popen is in progress.
        self._stopping.set()
        self.close()


class PendingQueue(asyncio.PriorityQueue):
    def promote(self, query, priority):
        # Updating the existing entry preserves both capacity and FIFO tie order.
        # An executing query is absent from the heap and is never preempted.
        for index, item in enumerate(self._queue):
            if item[2] == query and priority < item[0]:
                self._queue[index] = (priority, *item[1:])
                heapq.heapify(self._queue)
                break


class SharedEmbedding:
    def __init__(self, encoder):
        self.encoder = encoder
        self.cache = OrderedDict()
        self.pending = {}
        self.queue = PendingQueue(maxsize=24)
        self.sequence = itertools.count()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="search-embedding")
        self.task = None
        self._closed = False
        self._closing = None
        self._operation = None

    async def start(self):
        if self._closed:
            raise RuntimeError("임베딩 실행기가 종료되었습니다.")
        self._operation = asyncio.get_running_loop().run_in_executor(self.pool, self.encoder.start)
        self._operation.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        try:
            await asyncio.shield(self._operation)
            if self._closed:
                raise RuntimeError("임베딩 실행기가 종료되었습니다.")
        except BaseException:
            with suppress(Exception):
                await self.close()
            raise
        self.task = asyncio.create_task(self._work())

    async def _work(self):
        while True:
            _, _, query, future = await self.queue.get()
            try:
                self._operation = asyncio.get_running_loop().run_in_executor(self.pool, self.encoder.encode, query)
                self._operation.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
                vector = await asyncio.shield(self._operation)
                self.cache[query] = vector
                if len(self.cache) > 2048:
                    self.cache.popitem(last=False)
                if not future.done():
                    future.set_result(vector)
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                self.pending.pop(query, None)
                self.queue.task_done()

    async def get(self, query, source="qa"):
        if self._closed:
            raise RuntimeError("임베딩 실행기가 종료되었습니다.")
        if query in self.cache:
            self.cache.move_to_end(query)
            return self.cache[query], True
        future = self.pending.get(query)
        priority = {"chat": 0, "qa": 1, "profile": 2}.get(source, 1)
        if future is None:
            if self.queue.full():
                raise SearchBusy("검색 요청이 많습니다. 잠시 후 다시 검색해 주세요.")
            future = asyncio.get_running_loop().create_future()
            # Consume orphaned failures too when every waiting client disconnects.
            future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            self.pending[query] = future
            self.queue.put_nowait((priority, next(self.sequence), query, future))
        else:
            self.queue.promote(query, priority)
        return await asyncio.wait_for(asyncio.shield(future), timeout=20), False

    async def close(self):
        if self._closing is None:
            self._closed = True
            self._closing = asyncio.create_task(self._close())
        await asyncio.shield(self._closing)

    async def _close(self):
        for future in self.pending.values():
            if not future.done():
                future.set_exception(RuntimeError("임베딩 실행기가 종료되었습니다."))
        self.pending.clear()
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        try:
            stop = getattr(self.encoder, 'shutdown', self.encoder.close)
            await asyncio.wait_for(asyncio.to_thread(stop), timeout=5)
            if self._operation is not None:
                with suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(self._operation), timeout=1)
        finally:
            self.pool.shutdown(wait=False, cancel_futures=True)
