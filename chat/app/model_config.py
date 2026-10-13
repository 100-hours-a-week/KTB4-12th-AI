"""OpenRouter model capabilities; no paid probes or model fallbacks."""
import asyncio
import json
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import httpx
from app.errors import APIError

DEFAULT_MODEL = 'anthropic/claude-haiku-5.5'
PINNED_PROVIDERS = {'qwen/qwen3.8-27b': 'Cerebras'}
ACTIVE_MODEL = ContextVar('chat_model', default=None)


class ModelOutputError(ValueError):
    """No automatic paid retry for exhausted or empty model output."""


def normalize_model(value):
    value = value.strip()
    if len(value) > 160 or not re.fullmatch(r'[A-Za-z0-9_-]+/[A-Za-z0-9][A-Za-z0-9_.:-]*', value):
        raise ValueError('OpenRouter 모델 ID는 provider/model 형식이어야 합니다.')
    return value


@dataclass(frozen=True)
class ModelConfig:
    model: str
    parameters: frozenset
    reasoning: dict
    mandatory: bool
    max_output: int
    provider: str | None = None

    @classmethod
    def from_metadata(cls, model, data):
        provider = PINNED_PROVIDERS.get(model)
        endpoint = data.get('provider_endpoint') if provider else None
        if provider and (not endpoint or endpoint.get('provider_name') != provider):
            raise APIError(503, 'Cerebras 모델 정보를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.')
        architecture = data.get('architecture') or {}
        if 'text' not in architecture.get('input_modalities', []) or 'text' not in architecture.get('output_modalities', []):
            raise APIError(400, '텍스트 대화를 지원하는 OpenRouter 모델을 선택해 주세요.')
        parameters = frozenset(data.get('supported_parameters') or [])
        if endpoint:
            parameters &= frozenset(endpoint.get('supported_parameters') or [])
        capabilities = data.get('reasoning')
        mandatory = bool(capabilities and capabilities.get('mandatory'))
        reasoning = {}
        if 'reasoning' in parameters:
            reasoning = {'exclude': True}
            if capabilities is not None and capabilities.get('mandatory') is False:
                reasoning['enabled'] = False
                if 'none' in (capabilities.get('supported_efforts') or []): reasoning['effort'] = 'none'
                elif model == 'anthropic/claude-haiku-5.5' and 'low' in (capabilities.get('supported_efforts') or []): reasoning['effort'] = 'low'
            elif capabilities:
                efforts = capabilities.get('supported_efforts', [])
                if efforts is None: efforts = ['minimal', 'low']
                effort = next((e for e in ['minimal', 'low', 'medium', 'high', 'xhigh', 'max'] if e in efforts), None)
                if effort: reasoning['effort'] = effort
                # Unknown/mandatory reasoning must never be disabled.
        maximum = (data.get('top_provider') or {}).get('max_completion_tokens') or 8192
        if endpoint: maximum = min(maximum, endpoint.get('max_completion_tokens') or maximum)
        return cls(model, parameters, reasoning, mandatory, int(maximum), provider)

    def request(self, messages, limit, temperature):
        # Cerebras accepts a single leading system message in its chat template.
        if self.provider == 'Cerebras':
            leading = 0
            while leading < len(messages) and messages[leading].get('role') == 'system':
                leading += 1
            if leading > 1:
                messages = [{'role': 'system', 'content': '\n\n'.join(m['content'] for m in messages[:leading])}] + messages[leading:]
        thinking = self.mandatory or bool(self.reasoning) and self.reasoning.get('enabled') is not False
        body = {'model': self.model, 'messages': messages,
                'max_tokens': min(self.max_output, max(4096, limit + 2048) if thinking else limit)}
        if self.reasoning: body['reasoning'] = dict(self.reasoning)
        if self.provider:
            body['provider'] = {'only': [self.provider], 'allow_fallbacks': False, 'require_parameters': True}
        # Claude adaptive thinking rejects a fixed sampling temperature.
        if 'temperature' in self.parameters and not thinking: body['temperature'] = temperature
        return body

    def public(self):
        effort = self.reasoning.get('effort')
        label = '추론 끔' if self.reasoning.get('enabled') is False else ('추론 ' + (effort or '기본값') if self.reasoning else '추론 없음')
        if self.mandatory: label += ' · 필수'
        return {'model': self.model, 'reasoning': self.reasoning, 'reasoningMandatory': self.mandatory,
                'reasoningLabel': label, 'structuredOutput': 'structured_outputs' in self.parameters,
                'provider': self.provider}


class ModelCatalog:
    TTL = 3600

    def __init__(self, http):
        self.http = http
        self.cache = {}
        self.lock = asyncio.Lock()
        # Bootstrap the deployed defaults using an explicitly dated metadata snapshot.
        for data in json.loads(Path(__file__).with_name('data').joinpath('model-capabilities.json').read_text())['models']:
            self.cache[data['id']] = (time.monotonic(), ModelConfig.from_metadata(data['id'], data))

    def cached(self, model):
        item = self.cache.get(model)
        return item[1] if item else None

    async def resolve(self, model):
        try: model = normalize_model(model)
        except ValueError as exc: raise APIError(400, str(exc)) from None
        async with self.lock:
            item = self.cache.get(model)
            if item and time.monotonic() - item[0] < self.TTL: return item[1]
            try:
                response = await self.http.get('/model/' + model, timeout=10)
                if response.status_code == 404: raise APIError(400, 'OpenRouter 모델 ID를 찾을 수 없습니다. 복사한 ID를 확인해 주세요.')
                response.raise_for_status()
                data = response.json()['data']
                if model in PINNED_PROVIDERS:
                    response = await self.http.get('/models/' + model + '/endpoints', timeout=10)
                    response.raise_for_status()
                    endpoints = response.json()['data']['endpoints']
                    endpoint = next((e for e in endpoints if e.get('provider_name') == PINNED_PROVIDERS[model] and e.get('status') == 0), None)
                    if endpoint is None: raise APIError(503, 'Cerebras 공급자를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.')
                    data = {**data, 'provider_endpoint': endpoint}
                config = ModelConfig.from_metadata(model, data)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise APIError(503, '모델 정보를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.') from None
            if len(self.cache) >= 128: self.cache.pop(next(iter(self.cache)))
            self.cache[model] = (time.monotonic(), config)
            return config
