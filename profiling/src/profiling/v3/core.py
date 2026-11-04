"""v3 몸통 — **Modal v3 컨테이너 안에서** 돈다(같은 컨테이너의 vLLM 을 부르고 Laya 로 거름). AI 서버는 이것을 한 번 부른다(engine.py).

  버전 3   12B 나눠 뽑기(취향 글 · 리뷰 따로 · 함께 보냄, tag3disliked 프롬프트 · JSON 강제) → 비선호마다 질문 판정(예 확률 ≥ 0.1) → Laya 거부
  (11-04 버전 25 — 뽑기 LoRA 줄 형식 + 판정 LoRA — 는 버림: 처음 보는 자유글에서 진짜 싫어함 55/100, 버전 3 82/100)
돌려주는 태그는 규칙 A·B·C **전** — 규칙 C 의 그래프는 카탈로그가 있는 AI 서버가 건다.

계약(POST /v3/tags, CONTRACT 판):
  요청  {"contract": 1, "spec": "<규격 판>", "version": "v3", "timeout_s": 초,
         "text": 가드레일 정리 글(JSON 꼬리 없음), "parts": [{"prompt", "ratings", "has_pref"}, …](버전 3 나눠 뽑기 — 취향 글만 · 리뷰만)}
  응답  200 {"contract": 1, "tags": [{axis · tag · ref · evidence · polarity}], "calls": n, "usage": {"input_tokens", "output_tokens", "model_ms"}}
        409 {"error": "contract_mismatch" | "spec_mismatch" | "bad_version"}   500 {"error": "<V3Unavailable 종류>"}
        version 칸은 "v3" 하나만 받는다(11-04 버전 25 를 빼며 칸은 남김 — AI 서버와 Modal 앱은 따로 올라가므로, 칸을 없애면
        새 서버 → 옛 앱이 bad_version 으로 모두 v2 폴백. 계약 판도 그대로 1).
글 · 태그는 로그에 남기지 않는다(종류 · 개수 · 시간만).
실험(11-02 A100 · 11-03 vLLM 시험, chain_a100_1102.person)과 같은 순서 · 같은 문턱 — 기록 94건 되풀이로 같은 태그 확인(11-03).
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from profiling import tagging
from profiling.v3 import text as tx
from profiling.v3.client import Deadline, Gen, V3Client, V3Unavailable, load_grammar
from profiling.v3.laya import NEG, Laya, argmax_label

log = logging.getLogger("profiling.v3.core")

CONTRACT = 1
VERSION = "v3"
JUDGE_P = 0.1                                   # 버전 3 질문 판정 문턱(예 확률)
KEYS = ("axis", "tag", "ref", "evidence", "polarity")
MODELS = {"base": "base"}        # vLLM --served-model-name. 버전 3 뽑기 LoRA 를 끼우면 --lora-modules 이름을 여기 더하고 _v3 의 뽑기만 그 이름으로


class _Acc:
    def __init__(self) -> None:
        self.inp = self.out = self.calls = 0
        self.ms = 0.0

    def add(self, g: Gen) -> Gen:
        self.inp += g.input_tokens or 0
        self.out += g.output_tokens or 0
        self.ms += g.ms
        self.calls += 1
        return g


class Core:
    def __init__(self, client: V3Client, laya: Laya, spec: tagging.TagSpec, models: dict[str, str] | None = None) -> None:
        if not spec.disliked:
            raise ValueError(f"v3 는 비선호 규격(tag3disliked)만 — 지금 {spec.name}")
        self.client, self.laya, self.spec, self.models = client, laya, spec, {**MODELS, **(models or {})}
        self.grammar = load_grammar(spec.schema, spec.name) if client.structured == "grammar" else None

    def handle(self, req: dict) -> tuple[int, dict]:
        """요청 하나 → (HTTP 상태, 본문). 예외를 밖으로 내지 않는다."""
        if req.get("contract") != CONTRACT:
            return 409, {"error": "contract_mismatch"}
        if req.get("spec") != self.spec.version:
            return 409, {"error": "spec_mismatch"}
        if req.get("version") != VERSION:
            return 409, {"error": "bad_version"}
        dl, acc = Deadline(float(req.get("timeout_s") or 240.0)), _Acc()
        try:
            text = req["text"]
            if not isinstance(text, str):
                raise TypeError("text")
            keep = self._v3(text, req.get("parts") or [], dl, acc)
        except V3Unavailable as e:
            log.warning("v3_core fail:%s calls=%d", e.kind, acc.calls)
            return 500, {"error": e.kind}
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            log.warning("v3_core bad_request:%s", type(e).__name__)
            return 409, {"error": "bad_request"}
        log.info("v3_core ok calls=%d in_tok=%d out_tok=%d model_ms=%.0f tags=%d", acc.calls, acc.inp, acc.out, acc.ms, len(keep))
        return 200, {"contract": CONTRACT, "tags": [{k: t.get(k) or "" for k in KEYS} for t in keep], "calls": acc.calls,
                     "usage": {"input_tokens": acc.inp, "output_tokens": acc.out, "model_ms": round(acc.ms, 1)}}

    def _laya(self, xs: list[str]) -> list[tuple[list[float], list[float]]]:
        try:
            return self.laya.predict(xs)
        except Exception as e:  # Laya 가 깨지면 그 사람은 v2 로(원문을 남기지 않게 종류만)
            raise V3Unavailable("laya_unavailable", type(e).__name__) from e

    def _v3(self, inp: str, parts: list[dict], dl: Deadline, acc: _Acc) -> list[dict]:
        spec = self.spec

        def extract(p: dict) -> tuple[list[dict], Gen]:
            g = self.client.gen(self.models["base"], [{"role": "system", "content": spec.system}, {"role": "user", "content": p["prompt"]}],
                                max_tokens=1024, enable_thinking=False, deadline=dl, schema=spec.schema, grammar=self.grammar)
            parsed, _ = tagging.parse_report(tx.answer_text(g.text))
            return tagging.validate(parsed, p["ratings"], p["has_pref"], spec)[0], g

        # 11-03: 시간 = 출력 토큰 × 18ms 라 취향 글 · 리뷰 두 조각을 함께 보낸다(합이 아니라 긴 쪽만큼). 순서는 parts 그대로(merge 가 앞쪽을 먼저 봄)
        with ThreadPoolExecutor(max(1, len(parts))) as ex:
            outs = list(ex.map(extract, parts))
        got = []
        for k, g in outs:
            acc.add(g)
            got.append(k)
        keep = tx.merge(*got) if len(got) == 2 else (got[0] if got else [])
        negs = [c for c in keep if c.get("polarity") == "비선호"]
        if not negs:
            return keep
        bad = set()
        for c in negs:                           # 판정은 1토큰이라 차례로 불러도 거의 0초(11-03 동시 판 효과 없음) — 앞 글 캐시를 이어 씀
            p, g = self.client.yes_prob(self.models["base"], [
                {"role": "system", "content": tx.JUDGE_SYSTEM},
                {"role": "user", "content": f"글:\n{inp}\n\n문장: {tx.judge_question(c)}\n답(예/아니오):"}], deadline=dl)
            acc.add(g)
            if p < JUDGE_P:
                bad.add(id(c))
        for c, (pr, _) in zip(negs, self._laya([tx.laya_v3(inp, c) for c in negs])):
            if argmax_label(pr) in NEG:
                bad.add(id(c))                   # Laya 거부(남 얘기 · 습관·사실)
        return [c for c in keep if id(c) not in bad]
