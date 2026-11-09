"""성능 측정 대역(PROFILING_TAGGER_STUB) — 통합 런타임의 공급자 추출기에 대역 전송을 끼웠을 때:
키 없이 고정 답이 나오고, 주소 · 카탈로그 세대 검사(PreflightTransport)는 그대로 돈다."""

import pytest

from integration.profiler_boundary import ProviderLimitedExtractor
from profiling import llm_stub
from profiling.ports import TagExtractError

URL = "https://openrouter.ai/api/v1"


def _stub():
    return llm_stub.StubTransport(llm_stub.StubDelay(4.1, 6.9, "none"), sleep=lambda s: None)


def test_stub_reply_without_key_source():
    ex = ProviderLimitedExtractor(URL, "", llm_stub.STUB_MODEL, transport=_stub())
    checks = []
    ex.bind_generation(lambda: checks.append("checked"))
    reply = ex.extract("system", "prompt", {"type": "object"})
    assert reply.text == llm_stub.STUB_REPLY
    assert checks == ["checked"]                                  # 세대 검사는 대역에서도 요청마다


def test_generation_change_still_blocks_before_stub():
    ex = ProviderLimitedExtractor(URL, "", llm_stub.STUB_MODEL, transport=_stub())

    def changed():
        raise RuntimeError("generation changed")
    ex.bind_generation(changed)
    with pytest.raises(TagExtractError):
        ex.extract("system", "prompt", {"type": "object"})


def test_unofficial_address_still_refused():
    with pytest.raises(ValueError):
        ProviderLimitedExtractor("https://example.invalid/v1", "", llm_stub.STUB_MODEL, transport=_stub())
