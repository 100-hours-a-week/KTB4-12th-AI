"""Synthetic-only response evidence around the real Profiler transport."""
import json
import os
from pathlib import Path

if os.getenv('INTEGRATION_SYNTHETIC_ONLY') != '1':
    raise RuntimeError('Synthetic validation module only')

from integration.profiler_boundary import PreflightTransport
from integration.profiler_runtime import app

original = PreflightTransport.handle_request


def record(self, request):
    response = original(self, request)
    evidence = {'status': response.status_code}
    if response.status_code == 200:
        payload = response.json()
        for field in ('model', 'provider', 'usage', 'choices'):
            evidence[field] = payload.get(field)
    with Path(os.environ['SYNTHETIC_LLM_TRACE']).open('a') as stream:
        stream.write(json.dumps(evidence, ensure_ascii=False)+'\n')
    return response


PreflightTransport.handle_request = record
