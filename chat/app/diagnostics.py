"""Server-only execution evidence. Capture returned reasoning, never reconstruct it."""
import asyncio
import copy
import json
import os
import platform
import sys
import time
from contextvars import ContextVar
from datetime import datetime, timezone

ACTIVE_CALL = ContextVar('diagnostic_model_call', default=None)
FIELD_LIMIT = 262_144
RECORD_LIMIT = 512

def timestamp():
    return datetime.now(timezone.utc).isoformat()

def clean(value):
    """Remove credentials, including ones accidentally echoed in provider output."""
    secrets = [v for k, v in os.environ.items() if len(v) >= 8 and
               any(word in k.upper() for word in ('TOKEN', 'PASSWORD', 'SECRET', 'API_KEY'))]
    def visit(v):
        if isinstance(v, dict):
            return {k: '[REDACTED]' if k.lower() in ('authorization', 'api_key', 'apikey', 'password', 'secret', 'access_token') else visit(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)): return [visit(x) for x in v]
        if isinstance(v, str):
            for secret in secrets: v = v.replace(secret, '[REDACTED]')
        return v
    return visit(copy.deepcopy(value))

def bounded(value):
    value = clean(value)
    wire = json.dumps(value, ensure_ascii=False)
    if len(wire) <= FIELD_LIMIT: return value
    return {'truncated': True, 'originalChars': len(wire), 'jsonPrefix': wire[:FIELD_LIMIT]}

def evidence(receipt):
    if 'diagnostics' in receipt: return receipt['diagnostics']
    return receipt.setdefault('diagnostics', {'schema': 'agent-execution-v1', 'startedAt': timestamp(),
                                             'runtimeConditions': {'platform': platform.platform(), 'python': sys.version.split()[0],
                                               'processId': os.getpid(), 'cpuCount': os.cpu_count(), 'chatMaxActive': os.getenv('CHAT_MAX_ACTIVE')},
                                             'modelCalls': [], 'operations': [], 'timeline': []})

def append(receipt, group, item):
    data = evidence(receipt)
    if len(data[group]) < RECORD_LIMIT: data[group].append(item)
    else: data['truncated'] = True

def event(receipt, kind, value):
    append(receipt, 'timeline', {'at': timestamp(), 'kind': kind, 'data': bounded(value)})

class ModelCall:
    def __init__(self, receipt, provider):
        self.receipt = receipt
        self.start = time.monotonic()
        self.data = {'index': len(evidence(receipt)['modelCalls']) + 1, 'provider': provider,
                     'startedAt': timestamp(), '_startedMonotonic': self.start,
                     'turnOffsetMs': round((self.start - receipt['startedMonotonic']) * 1000, 2) if 'startedMonotonic' in receipt else None,
                     'status': 'running', 'request': None,
                     'firstPacketMs': None, 'firstTokenMs': None, 'firstReasoningMs': None, 'firstContentMs': None, 'firstToolCallMs': None,
                     'response': {'content': '', 'reasoning': '', 'reasoningDetails': [], 'toolCalls': [], 'finishReasons': []}}
        append(receipt, 'modelCalls', self.data)
        self.token = ACTIVE_CALL.set(self)

    def request(self, body, reservation):
        self.data['request'] = bounded(body)
        self.data['reservationId'] = reservation

    def packet(self, packet):
        elapsed = round((time.monotonic() - self.start) * 1000, 2)
        if self.data['firstPacketMs'] is None: self.data['firstPacketMs'] = elapsed
        response = self.data['response']
        for key in ('id', 'model', 'system_fingerprint'):
            if packet.get(key) is not None: response[key] = bounded(packet[key])
        if packet.get('error'): response['error'] = bounded(packet['error'])
        if isinstance(packet.get('usage'), dict): response['usage'] = bounded(packet['usage'])
        for choice in packet.get('choices', []):
            delta = choice.get('delta') or {}
            reasoning = bool(delta.get('reasoning_content') or delta.get('reasoning') or delta.get('reasoning_details'))
            content = bool(delta.get('content'))
            tool = bool(delta.get('tool_calls'))
            for key, present in [('firstTokenMs', reasoning or content or tool), ('firstReasoningMs', reasoning),
                                 ('firstContentMs', content), ('firstToolCallMs', tool)]:
                if present and self.data[key] is None: self.data[key] = elapsed
            for field, target in [('content', 'content'), ('reasoning_content', 'reasoning'), ('reasoning', 'reasoning')]:
                if isinstance(delta.get(field), str):
                    value = response[target] + delta[field]
                    if len(value) > FIELD_LIMIT: self.data['truncated'] = True
                    response[target] = value[:FIELD_LIMIT]
            details = delta.get('reasoning_details')
            if details:
                size = len(json.dumps(details, ensure_ascii=False))
                used = self.data.get('reasoningDetailsChars', 0)
                if used + size <= FIELD_LIMIT:
                    response['reasoningDetails'].append(bounded(details))
                    self.data['reasoningDetailsChars'] = used + size
                else: self.data['truncated'] = True
            for fragment in delta.get('tool_calls', []):
                index = fragment.get('index', 0)
                call = next((c for c in response['toolCalls'] if c['index'] == index and c['choiceIndex'] == choice.get('index', 0)), None)
                if call is None:
                    if len(response['toolCalls']) >= RECORD_LIMIT: self.data['truncated'] = True; continue
                    call = {'index': index, 'choiceIndex': choice.get('index', 0), 'id': '', 'name': '', 'argumentsRaw': ''}
                    response['toolCalls'].append(call)
                if fragment.get('id'): call['id'] = fragment['id']
                for field, target in [('name', 'name'), ('arguments', 'argumentsRaw')]:
                    value = (fragment.get('function') or {}).get(field)
                    if isinstance(value, str):
                        combined = call[target] + value
                        if len(combined) > FIELD_LIMIT: self.data['truncated'] = True
                        call[target] = combined[:FIELD_LIMIT]
            if choice.get('finish_reason') is not None: response['finishReasons'].append(choice['finish_reason'])
            if choice.get('error'): response['error'] = bounded(choice['error'])

    def finish(self, error=None):
        response = self.data['response']
        response['reasoningAvailability'] = 'returned_text' if response['reasoning'] else 'returned_details' if response['reasoningDetails'] else 'not_returned'
        for call in response['toolCalls']:
            try: call['arguments'] = json.loads(call['argumentsRaw'])
            except (ValueError, TypeError): call['argumentsParseError'] = True
        self.data.update(status='cancelled' if isinstance(error, asyncio.CancelledError) else 'failed' if error else 'completed',
                         endedAt=timestamp(), elapsedMs=round((time.monotonic() - self.start) * 1000, 2), elapsedFinal=True)
        self.data.pop('_startedMonotonic', None)
        if error: self.data['errorType'] = type(error).__name__
        safe = clean(self.data)
        self.data.clear(); self.data.update(safe)
        ACTIVE_CALL.reset(self.token)

async def operation(receipt, name, args, invoke):
    start = time.monotonic()
    record = {'name': name, 'arguments': bounded(args), 'startedAt': timestamp(), '_startedMonotonic': start, 'status': 'running',
              'turnOffsetMs': round((start - receipt['startedMonotonic']) * 1000, 2) if 'startedMonotonic' in receipt else None}
    append(receipt, 'operations', record)
    try:
        value = await invoke()
        record.update(status='completed', result=bounded(value))
        return value
    except BaseException as exc:
        record.update(status='cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed', errorType=type(exc).__name__)
        raise
    finally:
        record.update(endedAt=timestamp(), elapsedMs=round((time.monotonic() - start) * 1000, 2), elapsedFinal=True)
        record.pop('_startedMonotonic', None)

def sync_operation(receipt, name, args, invoke, result_view):
    start = time.monotonic()
    record = {'name': name, 'arguments': bounded(args), 'startedAt': timestamp(), 'status': 'running',
              'turnOffsetMs': round((start - receipt['startedMonotonic']) * 1000, 2) if 'startedMonotonic' in receipt else None}
    append(receipt, 'operations', record)
    try:
        value = invoke()
        record.update(status='completed', result=bounded(result_view(value)))
        return value
    except Exception as exc:
        record.update(status='failed', errorType=type(exc).__name__)
        raise
    finally:
        record.update(endedAt=timestamp(), elapsedMs=round((time.monotonic() - start) * 1000, 2), elapsedFinal=True)

def snapshot_receipt(receipt):
    """Freeze elapsed times on this process's monotonic clock before storage RPC."""
    now = time.monotonic()
    value = copy.deepcopy(receipt)
    start = value.pop('startedMonotonic', None)
    if start is not None and not value.get('outcome'):
        value['elapsedMs'] = round((now - start) * 1000, 2)
    value['timingCapturedAt'] = timestamp()
    data = value.get('diagnostics', {})
    for record in data.get('modelCalls', []) + data.get('operations', []):
        start = record.pop('_startedMonotonic', None)
        if record['status'] == 'running' and start is not None:
            record.update(elapsedMs=round((now - start) * 1000, 2), elapsedFinal=False)
    return clean(value)

def execution_summary(context):
    data = context.get('diagnostics')
    if not data: return {'availability': 'not_recorded', 'modelCalls': 0, 'operations': 0, 'reasoningReturned': False}
    calls = data.get('modelCalls', [])
    return {'availability': 'recorded', 'modelCalls': len(calls), 'operations': len(data.get('operations', [])),
            'reasoningReturned': any(c['response'].get('reasoning') or c['response'].get('reasoningDetails') for c in calls),
            'truncated': bool(data.get('truncated') or any(c.get('truncated') or (c.get('request') or {}).get('truncated') for c in calls) or
                              any((o.get('arguments') or {}).get('truncated') or (o.get('result') or {}).get('truncated') for o in data.get('operations',[]) if isinstance(o.get('result',{}),dict)))}
