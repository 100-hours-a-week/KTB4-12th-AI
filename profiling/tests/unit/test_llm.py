"""llm.OllamaTagExtractor — 가짜 HTTP(MockTransport)로: 사용량 읽기 · 실패 분류. 실제 Ollama 는 부르지 않는다."""

import httpx
import pytest

from profiling.llm import OllamaTagExtractor
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
