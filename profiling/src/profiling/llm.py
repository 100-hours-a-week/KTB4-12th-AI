"""LLM 어댑터 — ports.TagExtractor 구현. 지금은 로컬 Ollama 하나(유료 API 없음).

결정성: temperature 0 · seed 고정 · 생각 모드 끔 · JSON 스키마 강제(Ollama `format`) — 같은 입력이면 같은 태그(10-12 실측: gemma4:12b ·
qwen3.5:9b 3회 반복 모두 동일). 시간: 사람당 p95 gemma4:12b 21초 · qwen3.5:9b 9초(10-12, Apple 실리콘 로컬) — 7.6 은 비동기라 BE 응답엔
영향이 없지만 워커 슬롯을 그만큼 잡는다.

사용량: Ollama 응답의 prompt_eval_count · eval_count(토큰)와 *_duration(나노초)을 TokenUsage 로 돌려준다 — 로컬이라 돈은 안 들지만
외부 API 로 바꿀 때의 비용 추정과 "왜 느렸나"(적재 · 입력 · 출력 중 어디서) 추적에 쓴다. 실패는 TagExtractError(kind)로 분류한다.
"""

from __future__ import annotations

import logging

import httpx

from profiling.ports import TagExtractError, TagReply, TokenUsage

log = logging.getLogger(__name__)


def _ms(ns: object) -> float | None:
    return round(ns / 1e6, 1) if isinstance(ns, int | float) else None


def usage_of(body: dict) -> TokenUsage:
    """Ollama /api/chat 응답 → TokenUsage. 없는 필드는 None(버전에 따라 빠질 수 있다)."""
    return TokenUsage(input_tokens=body.get("prompt_eval_count"), output_tokens=body.get("eval_count"),
                      total_ms=_ms(body.get("total_duration")), load_ms=_ms(body.get("load_duration")),
                      input_ms=_ms(body.get("prompt_eval_duration")), output_ms=_ms(body.get("eval_duration")),
                      stop=body.get("done_reason"))


class OllamaTagExtractor:
    def __init__(self, base_url: str, model: str, *, timeout_s: float = 120.0, seed: int = 42,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.model = model
        self._seed = seed
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport)

    def extract(self, system: str, prompt: str, schema: dict) -> TagReply:
        try:
            r = self._client.post("/api/chat", json={
                "model": self.model, "stream": False, "think": False, "format": schema,
                "options": {"temperature": 0, "seed": self._seed},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            })
        except httpx.TimeoutException as e:
            raise TagExtractError("timeout", f"model={self.model} {type(e).__name__}") from e
        except httpx.TransportError as e:            # 연결 거부 · DNS — Ollama 가 안 떠 있음
            raise TagExtractError("connect", f"model={self.model} {type(e).__name__}") from e
        if r.status_code >= 400:                      # 404 = 모델 없음(ollama pull 필요) · 500 = 메모리 부족 등. 본문은 Ollama 의 오류 문구(원문 없음)
            raise TagExtractError(f"http_{r.status_code}", f"model={self.model} {r.text[:200]}")
        try:
            body = r.json()
            text = body["message"]["content"]
        except (ValueError, KeyError, TypeError) as e:
            raise TagExtractError("bad_response", f"model={self.model} {type(e).__name__}") from e
        usage = usage_of(body)
        if usage.stop == "length":                    # 출력 상한에서 잘림 — JSON 이 깨졌을 가능성이 높다
            raise TagExtractError("truncated", f"model={self.model} output_tokens={usage.output_tokens}", usage)
        log.debug("ollama model=%s in_tok=%s out_tok=%s load_ms=%s in_ms=%s out_ms=%s total_ms=%s stop=%s", self.model,
                  usage.input_tokens, usage.output_tokens, usage.load_ms, usage.input_ms, usage.output_ms, usage.total_ms, usage.stop)
        return TagReply(text, usage)

    def warm_up(self) -> None:
        """모델을 메모리에 올려 둔다(첫 요청의 적재 시간을 요청 밖으로). 실패해도 괜찮다 — 첫 요청이 대신 올린다(로그엔 남긴다)."""
        try:
            self._client.post("/api/generate", json={"model": self.model, "prompt": "", "keep_alive": "30m"}).raise_for_status()
            log.info("태그 추출기 준비 model=%s", self.model)
        except httpx.HTTPError as e:
            log.warning("태그 추출기 미리 올리기 실패 model=%s: %s — 첫 요청이 대신 올린다(그 요청만 느림)", self.model, type(e).__name__)

    def close(self) -> None:
        self._client.close()
