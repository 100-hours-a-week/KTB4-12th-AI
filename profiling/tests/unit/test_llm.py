"""llm.OllamaTagExtractor — 가짜 HTTP(MockTransport)로: 사용량 읽기 · 실패 분류. 실제 Ollama 는 부르지 않는다."""

import httpx
import pytest

from profiling.llm import OllamaTagExtractor, OpenRouterTagExtractor
from profiling.ports import TagExtractError, TagExtractor

OK = {"message": {"content": '{"tags": []}'}, "done_reason": "stop", "prompt_eval_count": 812, "eval_count": 64,
      "total_duration": 1_500_000_000, "load_duration": 10_000_000, "prompt_eval_duration": 300_000_000, "eval_duration": 1_190_000_000}


def _tagger(handler) -> OllamaTagExtractor:
    return OllamaTagExtractor("http://ollama.test", "gemma4:12b", transport=httpx.MockTransport(handler))


def test_reply_carries_text_and_usage() -> None:
    reply = _tagger(lambda r: httpx.Response(200, json=OK)).extract("s", "p", {})
    assert isinstance(_tagger(lambda r: httpx.Response(200, json=OK)), TagExtractor)
    assert reply.text == '{"tags": []}'
    u = reply.usage
    assert (u.input_tokens, u.output_tokens, u.total_ms, u.load_ms, u.input_ms, u.output_ms, u.stop) == (812, 64, 1500.0, 10.0, 300.0, 1190.0, "stop")


def test_missing_usage_fields_are_none() -> None:
    reply = _tagger(lambda r: httpx.Response(200, json={"message": {"content": "{}"}})).extract("s", "p", {})
    assert reply.usage.input_tokens is None and reply.usage.total_ms is None


def _raise(exc):
    def handler(request):
        raise exc("boom", request=request)
    return handler


@pytest.mark.parametrize(("handler", "kind"), [
    (_raise(httpx.ReadTimeout), "timeout"),
    (_raise(httpx.ConnectError), "connect"),
    (lambda r: httpx.Response(404, json={"error": "model 'gemma4:12b' not found"}), "http_404"),
    (lambda r: httpx.Response(200, text="not json"), "bad_response"),
    (lambda r: httpx.Response(200, json={"done_reason": "stop"}), "bad_response"),
    (lambda r: httpx.Response(200, json={**OK, "done_reason": "length"}), "truncated"),
])
def test_failures_are_classified(handler, kind) -> None:
    with pytest.raises(TagExtractError) as e:
        _tagger(handler).extract("s", "p", {})
    assert e.value.kind == kind
    if kind == "truncated":
        assert e.value.usage.output_tokens == 64                    # 잘려도 사용량은 남긴다


def test_warm_up_failure_is_logged_not_raised(caplog) -> None:
    with caplog.at_level("WARNING", logger="profiling.llm"):
        _tagger(_raise(httpx.ConnectError)).warm_up()
    assert any("미리 올리기 실패" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- OpenRouter (외부 API) — 가짜 HTTP 로만, 실제 호출 없음

OR_OK = {"choices": [{"message": {"content": '```json\n{"tags": []}\n```'}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 900, "completion_tokens": 120, "cost": 0.00042}}


def _or(handler) -> OpenRouterTagExtractor:
    return OpenRouterTagExtractor("https://openrouter.test/api/v1", "sk-or-test-SECRET42", "vendor/model-x", transport=httpx.MockTransport(handler))


def test_openrouter_reply_usage_cost_and_fence_stripped() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"], seen["path"], seen["body"] = request.headers["authorization"], request.url.path, request.read()
        return httpx.Response(200, json=OR_OK)

    t = _or(handler)
    reply = t.extract("s", "p", {"type": "object"})
    assert isinstance(t, TagExtractor) and reply.text == '{"tags": []}'                    # ```json 감싸기 벗김
    u = reply.usage
    assert (u.input_tokens, u.output_tokens, u.cost_usd, u.stop) == (900, 120, 0.00042, "stop") and u.total_ms is not None
    assert seen["auth"] == "Bearer sk-or-test-SECRET42" and seen["path"] == "/api/v1/chat/completions"
    body = __import__("json").loads(seen["body"])
    assert body["model"] == "vendor/model-x" and body["temperature"] == 0 and body["usage"] == {"include": True}
    assert body["response_format"]["type"] == "json_schema"
    assert "SECRET42" not in repr(t)                                                           # 키는 repr 에 없다


@pytest.mark.parametrize(("handler", "kind"), [
    (lambda r: httpx.Response(401, json={"error": {"message": "No auth credentials found"}}), "http_401"),
    (lambda r: httpx.Response(402, json={"error": {"message": "Insufficient credits"}}), "http_402"),
    (lambda r: httpx.Response(429, json={"error": {"message": "rate limited"}}), "http_429"),
    (lambda r: httpx.Response(200, json={"choices": []}), "bad_response"),
    (lambda r: httpx.Response(200, json={**OR_OK, "choices": [{"message": {"content": "{"}, "finish_reason": "length"}]}), "truncated"),
    (_raise(httpx.ReadTimeout), "timeout"),
])
def test_openrouter_failures_are_classified_without_key(handler, kind) -> None:
    with pytest.raises(TagExtractError) as e:
        _or(handler).extract("s", "p", {})
    assert e.value.kind == kind and "SECRET42" not in str(e.value)


def test_settings_openrouter_key_is_secret(monkeypatch) -> None:
    from profiling.settings import Settings

    monkeypatch.setenv("PROFILING_TAGGER", "openrouter")
    monkeypatch.setenv("PROFILING_OPENROUTER_API_KEY", "sk-or-test-SECRET42")
    s = Settings(_env_file=None)
    assert s.TAGGER == "openrouter" and s.OPENROUTER_API_KEY.get_secret_value() == "sk-or-test-SECRET42"
    assert "SECRET42" not in repr(s) and "SECRET42" not in str(s.model_dump())
