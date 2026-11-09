"""태그 추출 대역(llm_stub) — 응답 시간 분포(p50 · p95 · 흔들림 방식) · 고정 답 · 바깥 연결 없음 · 설정 이름."""

import httpx
import pytest

from profiling import llm_stub
from profiling.llm import OpenRouterTagExtractor
from profiling.settings import Settings


def _quantiles(xs: list[float]) -> tuple[float, float]:
    xs = sorted(xs)
    return xs[len(xs) // 2], xs[int(0.95 * (len(xs) - 1))]


def test_none_is_always_p50() -> None:
    d = llm_stub.StubDelay(4.1, 6.9, "none")
    assert {d.sample() for _ in range(50)} == {4.1}


@pytest.mark.parametrize("jitter", ["lognormal", "uniform"])
def test_jitter_matches_p50_and_p95(jitter: str) -> None:
    d = llm_stub.StubDelay(4.1, 6.9, jitter, seed=7)
    p50, p95 = _quantiles([d.sample() for _ in range(40_000)])
    assert p50 == pytest.approx(4.1, rel=0.03)
    assert p95 == pytest.approx(6.9, rel=0.03)


def test_max_caps_and_never_negative() -> None:
    d = llm_stub.StubDelay(4.1, 6.9, "lognormal", max_s=5.0, seed=1)
    xs = [d.sample() for _ in range(5_000)]
    assert max(xs) == 5.0 and min(xs) > 0
    u = llm_stub.StubDelay(1.0, 9.0, "uniform", seed=1)          # 아래 끝이 0 아래로 가는 넓은 폭
    assert min(u.sample() for _ in range(5_000)) >= 0.0


def test_seed_repeats_sequence() -> None:
    a = llm_stub.StubDelay(4.1, 6.9, seed=3)
    b = llm_stub.StubDelay(4.1, 6.9, seed=3)
    assert [a.sample() for _ in range(20)] == [b.sample() for _ in range(20)]


@pytest.mark.parametrize(("p50", "p95", "jitter", "max_s"), [
    (0, 6.9, "lognormal", None),          # p50 0
    (4.1, 3.0, "lognormal", None),        # p95 < p50
    (4.1, 6.9, "gauss", None),            # 모르는 방식
    (4.1, 6.9, "lognormal", 2.0),         # 상한 < p50
])
def test_bad_settings_refuse(p50: float, p95: float, jitter: str, max_s: float | None) -> None:
    with pytest.raises(ValueError):
        llm_stub.StubDelay(p50, p95, jitter, max_s)


def test_extractor_gets_fixed_reply_without_network() -> None:
    waits: list[float] = []
    transport = llm_stub.StubTransport(llm_stub.StubDelay(4.1, 6.9, "none"), sleep=waits.append)
    ex = OpenRouterTagExtractor("https://openrouter.ai/api/v1", "", llm_stub.STUB_MODEL, transport=transport)
    reply = ex.extract("system", "prompt", {"type": "object"})
    assert reply.text == llm_stub.STUB_REPLY
    assert waits == [4.1]
    assert reply.usage.cost_usd == 0 and reply.usage.stop == "stop"


def test_stub_transport_never_opens_a_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise AssertionError("바깥 연결 시도")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    transport = llm_stub.StubTransport(llm_stub.StubDelay(0.01, 0.01, "none"), sleep=lambda s: None)
    with httpx.Client(transport=transport) as c:
        assert c.post("https://openrouter.ai/api/v1/chat/completions", json={}).status_code == 200


def test_settings_defaults_are_v3_measured_and_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for n in ("PROFILING_TAGGER_STUB", "PROFILING_TAGGER_STUB_P50_S", "PROFILING_TAGGER_STUB_P95_S",
              "PROFILING_TAGGER_STUB_JITTER", "PROFILING_TAGGER_STUB_MAX_S", "PROFILING_TAGGER_STUB_SEED"):
        monkeypatch.delenv(n, raising=False)
    s = Settings(_env_file=None)
    assert s.TAGGER_STUB is False
    assert (s.TAGGER_STUB_P50_S, s.TAGGER_STUB_P95_S, s.TAGGER_STUB_MAX_S, s.TAGGER_STUB_JITTER) == (4.1, 6.9, 12.8, "lognormal")


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFILING_TAGGER_STUB", "true")
    monkeypatch.setenv("PROFILING_TAGGER_STUB_P50_S", "3.6")
    monkeypatch.setenv("PROFILING_TAGGER_STUB_P95_S", "6.0")
    monkeypatch.setenv("PROFILING_TAGGER_STUB_JITTER", "uniform")
    monkeypatch.setenv("PROFILING_TAGGER_STUB_MAX_S", "")
    s = Settings(_env_file=None)
    t = llm_stub.from_settings(s)
    assert s.TAGGER_STUB and t.delay.describe() == "jitter=uniform p50=3.6s p95=6.0s"


def test_reply_passes_tag_validation_as_empty() -> None:
    from profiling import tagging
    assert tagging.parse(llm_stub.STUB_REPLY) == []


@pytest.mark.parametrize("env", ["production", "Production", " production "])
def test_production_refuses_stub(env: str) -> None:
    with pytest.raises(llm_stub.StubRefused):
        llm_stub.from_settings(Settings(_env_file=None), environ={"AI_ENVIRONMENT": env})


@pytest.mark.parametrize("environ", [{}, {"AI_ENVIRONMENT": "local"}, {"AI_ENVIRONMENT": "develop"}])
def test_non_production_allows_stub(environ: dict) -> None:
    assert isinstance(llm_stub.from_settings(Settings(_env_file=None), environ=environ), llm_stub.StubTransport)
