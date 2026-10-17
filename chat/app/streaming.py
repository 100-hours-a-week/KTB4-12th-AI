"""Read the provider independently; publish only committed, replayable deltas."""
import asyncio
import json
import time
from app.model_config import ModelOutputError

FLUSH_INTERVAL = 0.08
FLUSH_CHARS = 64


async def stream_answer(runtime, cid, tid, receipt, request):
    queue = asyncio.Queue(maxsize=64)
    started = receipt['startedMonotonic']
    metrics = receipt['streaming'] = {
        'flushIntervalMs': round(FLUSH_INTERVAL * 1000), 'flushChars': FLUSH_CHARS,
        'providerDeltas': 0, 'characters': 0, 'maxProviderGapMs': 0,
        'answerEvents': 0, 'storeMs': 0, 'eventOffsetsMs': [],
    }

    async def read_provider():
        finish = None
        previous = None
        try:
            async with runtime.http.stream('POST', '/chat/completions', json=request) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith('data:'): continue
                    value = line[5:].strip()
                    if value == '[DONE]': break
                    data = json.loads(value)
                    if data.get('error'): raise ValueError('Model stream error')
                    if data.get('usage'): receipt['usage'].append(data['usage'])
                    for choice in data.get('choices', []):
                        finish = choice.get('finish_reason') or finish
                        delta = choice.get('delta', {}).get('content') or ''
                        if not delta: continue
                        now = time.monotonic()
                        if previous is None:
                            receipt['firstAnswerMs'] = metrics['firstProviderDeltaMs'] = round((now - started) * 1000)
                        else:
                            metrics['maxProviderGapMs'] = max(metrics['maxProviderGapMs'], round((now - previous) * 1000))
                        previous = now
                        metrics['providerDeltas'] += 1
                        metrics['characters'] += len(delta)
                        await queue.put(('delta', delta))
            await queue.put(('end', finish))
        except Exception as exc:
            await queue.put(('error', exc))

    reader = asyncio.create_task(read_provider())
    buffer = ''
    deadline = None

    async def flush():
        nonlocal buffer, deadline
        before = time.monotonic()
        await runtime.emit(cid, tid, 'answer.delta', {'delta': buffer})
        now = time.monotonic()
        offset = round((now - started) * 1000)
        metrics.setdefault('firstCommittedDeltaMs', offset)
        metrics['answerEvents'] += 1
        metrics['storeMs'] += round((now - before) * 1000)
        if len(metrics['eventOffsetsMs']) < 256: metrics['eventOffsetsMs'].append(offset)
        buffer = ''
        deadline = None

    try:
        while True:
            if buffer and time.monotonic() >= deadline:
                await flush()
            try:
                if buffer:
                    async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                        kind, value = await queue.get()
                else:
                    kind, value = await queue.get()
            except TimeoutError:
                await flush()
                continue
            if kind == 'delta':
                if not buffer: deadline = time.monotonic() + FLUSH_INTERVAL
                buffer += value
                if metrics['answerEvents'] == 0 or len(buffer) >= FLUSH_CHARS: await flush()
                continue
            if buffer: await flush()
            if kind == 'error': raise value
            if value == 'length':
                raise ModelOutputError('추론·답변 토큰 한도에 도달해 답변이 중단됐습니다. 질문을 짧게 나누거나 다른 모델을 선택해 주세요.')
            if not metrics['providerDeltas'] or value not in {'stop', 'end_turn'}:
                raise ValueError('Incomplete model stream')
            return
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
