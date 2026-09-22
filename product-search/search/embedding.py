"""One managed encoder, bounded scheduling, shared LRU and single-flight queries."""
import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import itertools
import json
from pathlib import Path
import select
import subprocess
import time
import numpy as np


class SearchBusy(Exception):
    pass


class Encoder:
    def __init__(self, directory: Path):
        self.directory = directory
        self.process = None

    def start(self):
        self.process = subprocess.Popen(["node", "service.mjs"], cwd=self.directory,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        if not select.select([self.process.stdout], [], [], 60)[0]:
            self.close()
            raise RuntimeError("임베딩 모델 준비 시간 초과")
        if not json.loads(self.process.stdout.readline()).get("ready"):
            self.close()
            raise RuntimeError("임베딩 모델 준비 실패")

    def encode(self, query):
        if self.process is None or self.process.poll() is not None:
            self.close()
            self.start()
        self.process.stdin.write(json.dumps({"text": query}, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        if not select.select([self.process.stdout], [], [], 15)[0]:
            self.close()
            raise RuntimeError("임베딩 응답 시간 초과")
        response = json.loads(self.process.stdout.readline())
        vector = np.asarray(response.get("vector", []), dtype=np.float32)
        if vector.shape != (768,) or not np.isfinite(vector).all() or np.linalg.norm(vector) == 0:
            raise RuntimeError("임베딩 응답이 올바르지 않습니다.")
        return vector / np.linalg.norm(vector)

    def close(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None


class SharedEmbedding:
    def __init__(self, encoder):
        self.encoder = encoder
        self.cache = OrderedDict()
        self.pending = {}
        self.queue = asyncio.PriorityQueue(maxsize=24)
        self.sequence = itertools.count()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="search-embedding")
        self.task = None

    async def start(self):
        await asyncio.get_running_loop().run_in_executor(self.pool, self.encoder.start)
        self.task = asyncio.create_task(self._work())

    async def _work(self):
        while True:
            _, _, query, future = await self.queue.get()
            try:
                vector = await asyncio.get_running_loop().run_in_executor(self.pool, self.encoder.encode, query)
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
        if query in self.cache:
            self.cache.move_to_end(query)
            return self.cache[query], True
        future = self.pending.get(query)
        if future is None:
            if self.queue.full():
                raise SearchBusy("검색 요청이 많습니다. 잠시 후 다시 검색해 주세요.")
            future = asyncio.get_running_loop().create_future()
            # Consume orphaned failures too when every waiting client disconnects.
            future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            self.pending[query] = future
            self.queue.put_nowait(({"chat": 0, "qa": 1, "profile": 2}.get(source, 1), next(self.sequence), query, future))
        return await asyncio.wait_for(asyncio.shield(future), timeout=20), False

    async def close(self):
        await self.queue.join()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await asyncio.get_running_loop().run_in_executor(self.pool, self.encoder.close)
        self.pool.shutdown(wait=True)
