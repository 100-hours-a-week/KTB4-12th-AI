"""v3 태그 엔진(AI 서버 쪽) — 사람 한 명: 가드레일 정리 글을 Modal v3 컨테이너에 **한 번** 보내고(core.py 가 뽑기 · 판정 · Laya),
돌아온 태그에 규칙 A·B·C 를 건다. 검증기 · 저장 · 추천 30개는 pipeline 이 v2 와 같은 길로 한다.

AI 서버 밖으로 나가는 것은 v2 와 같은 가드레일 정리 글뿐(원문 아님). 규칙 C 의 그래프는 지금 활성 카탈로그로 만든다(판이 바뀌면 다시).
Modal 을 쓸 수 없으면(연결 · 5xx · 기한 · 규격 판이 다름 · 상대가 실패를 돌려줌) V3Unavailable — pipeline 이 v2 추출기로 넘긴다(사람 한 명 통째로).
"""
import threading
from collections.abc import Callable
from typing import NamedTuple

from profiling import tagging
from profiling.ports import TokenUsage
from profiling.schemas import ProductRecord
from profiling.types import ProfileRequest
from profiling.v3 import graph as gr
from profiling.v3 import rules
from profiling.v3 import text as tx
from profiling.v3.client import Deadline, V3Client, V3Unavailable
from profiling.v3.core import CONTRACT, KEYS, VERSION

MARGIN_S = 5.0                                  # Modal 쪽 기한은 이만큼 짧게 — 넘기기 전에 그쪽이 먼저 멈추고 답하게


class V3Run(NamedTuple):
    keep: list[dict]                            # 규칙까지 거친 태그(axis · tag · ref · evidence · polarity)
    usage: TokenUsage
    calls: int


def request_of(rq: ProfileRequest, by_id: dict[int, ProductRecord], spec: tagging.TagSpec, timeout_s: float) -> dict:
    """Modal 에 보낼 본문 — 전체 정리 글(판정 · Laya · 규칙이 봄) + 버전 3 나눠 뽑기 두 조각(취향 글만 · 리뷰만, 있는 것만)."""
    parts = []
    pref_only = tagging.prepare(rq, by_id, only=frozenset())
    if pref_only.has_pref:
        parts.append({"prompt": pref_only.prompt, "ratings": pref_only.ratings, "has_pref": True})
    rev_only = tagging.prepare(rq, by_id, with_pref=False)
    if rev_only.ratings:
        parts.append({"prompt": rev_only.prompt, "ratings": rev_only.ratings, "has_pref": False})
    return {"contract": CONTRACT, "spec": spec.version, "version": VERSION, "timeout_s": timeout_s,
            "text": tx.body_of(tagging.prepare(rq, by_id).prompt), "parts": parts}


class V3Engine:
    """pipeline 이 tagger 자리에 받는다. fallback = 쓸 수 없을 때 넘길 v2 추출기(TagExtractor, 없으면 None → 태그 없이)."""

    version = VERSION          # 로그 · 저장 판(prompt_version …+v3-v3@modal) — 11-04 버전 25 를 빼도 같은 글자(이미 저장된 사람이 다시 뽑히지 않게)

    def __init__(self, client: V3Client, spec: tagging.TagSpec, *, timeout_s: float = 240.0, fallback=None) -> None:
        if not spec.disliked:
            raise ValueError(f"v3 는 비선호 규격(tag3disliked)만 — 지금 {spec.name}")
        self.client, self.spec, self.timeout_s, self.fallback = client, spec, timeout_s, fallback
        self.model = f"v3-{VERSION}@modal"
        self._graph_key: object = None
        self._rule_c: Callable[[dict], dict] | None = None
        self._graph_lock = threading.Lock()
        self._preflight: Callable[[], object] | None = None

    def __repr__(self) -> str:
        return f"V3Engine(client={self.client!r}, fallback={self.fallback!r})"

    def bind_generation(self, check: Callable[[], object]) -> None:
        """배포 묶음(integration/profiler_runtime)이 기동 때 부른다 — 카탈로그 판이 바뀌었는지 보는 검사를 Modal 부르기 전에도 하고,
        폴백 추출기에도 넘긴다(폴백은 외부 AI 로 보내기 전에 같은 검사를 한다)."""
        self._preflight = check
        bind = getattr(self.fallback, "bind_generation", None)
        if callable(bind):
            bind(check)

    def close(self) -> None:
        self.client.close()
        close = getattr(self.fallback, "close", None)
        if callable(close):
            close()

    def rule_c_for(self, catalog_key: object, products: list[ProductRecord]) -> Callable[[dict], dict]:
        """규칙 C 그래프 — 카탈로그 판마다 한 번 만든다(지금 카탈로그로 1초 안쪽, 11-03)."""
        with self._graph_lock:
            if self._rule_c is None or self._graph_key != catalog_key:
                self._rule_c, self._graph_key = rules.make_rule_c(gr.build(products)), catalog_key
            return self._rule_c

    def run(self, rq: ProfileRequest, by_id: dict[int, ProductRecord], products: list[ProductRecord], *,
            catalog_key: object = None) -> V3Run:
        if self._preflight is not None:
            try:
                self._preflight()
            except Exception as e:  # 판이 바뀜 — Modal 에 보내지 않는다(폴백도 같은 검사에서 멈추고, 저장 단계가 FAILED 로 돌린다)
                raise V3Unavailable("generation_changed", type(e).__name__) from e
        dl = Deadline(self.timeout_s)
        body = request_of(rq, by_id, self.spec, max(1.0, self.timeout_s - MARGIN_S))
        got, ms = self.client.post_json("/v3/tags", body, dl, "v3")
        try:
            if got.get("contract") != CONTRACT:
                raise V3Unavailable("contract_mismatch")
            tags = [{k: str(t.get(k) or "") for k in KEYS} for t in got["tags"]]
            u = got.get("usage") or {}
            usage = TokenUsage(input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"), total_ms=ms)
            calls = int(got.get("calls") or 0)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise V3Unavailable("bad_response", type(e).__name__) from e
        keep = rules.apply(tags, body["text"], self.rule_c_for(catalog_key, products))
        return V3Run(keep, usage, calls)
