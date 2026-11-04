"""v3 HTTP 부르기 — 두 곳에서 쓴다.
  AI 서버 → Modal v3 컨테이너   post_json('/v3/tags') 한 번(사람 한 명) — engine.V3Engine
  Modal 안 조정 → vLLM(같은 컨테이너)   OpenAI 호환 /v1/chat/completions(gen · yes_prob) — core.Core

모델 이름 = vLLM `--served-model-name`(지금 base 하나 · LoRA 없음, 11-04 버전 25 의 extract · judge_stable 은 뺌).
JSON 강제(버전 3 추출)는 둘 중 하나:
  grammar      vLLM `structured_outputs.grammar` 에 xgrammar EBNF(tag3disliked.ebnf)를 보냄 — 실험(transformers · xgrammar)과 같은 글.
               vLLM 의 json_schema 는 띄어쓰기까지 없앤 JSON 을 강제해 모델 답이 달라졌다(11-03)
  json_schema  OpenAI 식 response_format(로컬 서버용 ⚠️ 실험 점수와 같은지 안 잼)

실패는 V3Unavailable(kind) 로 — 부르는 쪽이 v2(외부 API)로 넘긴다:
  connect · timeout(사람 한 명 기한 넘음) · http_<상태>(5xx 는 기한 안에서 다시 보냄 — 꺼진 서버가 켜지는 동안 Modal 이 5xx 를 줄 수 있음)
  · bad_response · no_logprobs. 켜지는 데 2분쯤 걸리는 것은 실패가 아니다(기한 안이면 기다림).
AI 서버 → Modal 은 Modal 프록시 인증(Modal-Key · Modal-Secret 헤더, 컨테이너를 켜기 전에 Modal 이 확인) + Bearer 키(컨테이너 안에서 확인).
키는 헤더에만 싣고 로그 · 예외 · repr 에 남기지 않는다. 원문(프롬프트 · 출력)도 예외에 넣지 않는다.
"""
import json
import math
import time
from pathlib import Path
from typing import Any, NamedTuple

import httpx

GRAMMAR_DIR = Path(__file__).resolve().parent
RETRY_STATUS = (502, 503, 504)
RETRY_WAIT_S = 2.0
CONNECT_TRIES = 3
MAX_REDIRECTS = 5      # Modal 은 150초마다 303 — 기한 240초면 두세 번


class V3Unavailable(Exception):
    """v3 모델 서버(또는 Laya)를 쓸 수 없음 — kind: connect · timeout · http_<상태> · bad_response · no_logprobs · laya_unavailable."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


class Gen(NamedTuple):
    text: str
    input_tokens: int | None
    output_tokens: int | None
    hit_max: bool
    ms: float


class Deadline:
    """사람 한 명의 v3 기한 — 호출마다 남은 시간을 타임아웃으로 쓴다."""

    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    def left(self) -> float:
        return self.end - time.monotonic()


def _timeout(left: float) -> httpx.Timeout:
    return httpx.Timeout(left, connect=min(left, 10.0))


def load_grammar(schema: dict[str, Any], name: str = "tag3disliked") -> str:
    """저장해 둔 EBNF — 규격의 스키마가 EBNF 를 만들 때와 같을 때만. 다르면 ValueError(규격을 바꿨으면 EBNF 를 다시 만들 것)."""
    saved = (GRAMMAR_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
    if json.dumps(schema, ensure_ascii=False) != saved:
        raise ValueError(f"v3 문법이 규격 스키마와 다름({name}) — xgrammar(any_whitespace=False)로 {name}.ebnf 를 다시 만들어야 함")
    return (GRAMMAR_DIR / f"{name}.ebnf").read_text(encoding="utf-8")


class V3Client:
    def __init__(self, base_url: str, api_key: str = "", *, proxy_key: str = "", proxy_secret: str = "", structured: str = "grammar",
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep) -> None:
        if structured not in ("grammar", "json_schema"):
            raise ValueError(f"structured={structured!r}")
        self.structured = structured
        self._sleep = sleep
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        if proxy_key or proxy_secret:
            headers |= {"Modal-Key": proxy_key, "Modal-Secret": proxy_secret}
        self._client = httpx.Client(base_url=base_url.rstrip("/"), transport=transport, trust_env=False, follow_redirects=False,
                                    headers=headers)   # 따라가기는 post_json 이 남은 기한으로(자동이면 기한을 처음부터 다시 셈)

    def __repr__(self) -> str:
        return f"V3Client(base_url={str(self._client.base_url)!r})"

    def post_json(self, path: str, body: dict, deadline: Deadline, label: str = "") -> tuple[dict, float]:
        """JSON 한 번 — 연결 실패는 몇 번, 502 · 503 · 504(켜지는 중)는 기한 안에서 다시. 그 밖의 4xx · 5xx 는 바로 V3Unavailable
        (상대가 {"error": 종류} 를 주면 kind = remote:<종류>).
        Modal 웹 주소는 150초가 넘으면 303 으로 같은 결과를 GET 하게 한다 — 따라갈 때마다 **남은** 기한을 쓴다
        (11-03: httpx 자동 따라가기가 기한을 처음부터 다시 세서 240초 기한에 263초 기다림)."""
        t0 = time.perf_counter()
        tries = 0
        label = label or path
        while True:
            left = deadline.left()
            if left <= 0:
                raise V3Unavailable("timeout", label)
            try:
                r = self._client.post(path, json=body, timeout=_timeout(left))
                hops = 0
                while r.is_redirect and r.next_request is not None:
                    hops += 1
                    left = deadline.left()
                    if left <= 0 or hops > MAX_REDIRECTS:
                        raise V3Unavailable("timeout", f"{label} redirect {hops}")
                    nxt = r.next_request
                    nxt.extensions["timeout"] = _timeout(left).as_dict()
                    r = self._client.send(nxt)
            except httpx.TimeoutException as e:
                raise V3Unavailable("timeout", f"{label} {type(e).__name__}") from e
            except httpx.TransportError as e:
                tries += 1
                if tries >= CONNECT_TRIES or deadline.left() <= RETRY_WAIT_S:
                    raise V3Unavailable("connect", f"{label} {type(e).__name__}") from e
                self._sleep(RETRY_WAIT_S)
                continue
            if r.status_code in RETRY_STATUS and deadline.left() > RETRY_WAIT_S:
                self._sleep(RETRY_WAIT_S)
                continue
            if r.status_code >= 400:
                try:
                    err = r.json().get("error")
                except (ValueError, AttributeError):
                    err = None
                raise V3Unavailable(f"remote:{err}" if isinstance(err, str) and err else f"http_{r.status_code}", label)
            try:
                return r.json(), round((time.perf_counter() - t0) * 1000, 1)
            except ValueError as e:
                raise V3Unavailable("bad_response", f"{label} {type(e).__name__}") from e

    def _post(self, body: dict, deadline: Deadline) -> tuple[dict, float]:
        return self.post_json("/v1/chat/completions", body, deadline, f"model={body['model']}")

    def gen(self, model: str, messages: list[dict], *, max_tokens: int, enable_thinking: bool, deadline: Deadline,
            schema: dict[str, Any] | None = None, grammar: str | None = None) -> Gen:
        """한 번 생성 — temperature 0. schema 를 주면 JSON 강제(structured 방식대로). 생각 칸 · 특수 토큰은 살려 받는다(text.answer_text 가 읽음)."""
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0,
                                "chat_template_kwargs": {"enable_thinking": enable_thinking}, "skip_special_tokens": False}
        if schema is not None:
            if self.structured == "grammar":
                if grammar is None:
                    raise ValueError("structured=grammar 인데 문법이 없음")
                body["structured_outputs"] = {"grammar": grammar}
            else:
                body["response_format"] = {"type": "json_schema", "json_schema": {"name": "tags", "strict": False, "schema": schema}}
        b, ms = self._post(body, deadline)
        try:
            ch = b["choices"][0]
            u = b.get("usage") or {}
            return Gen(ch["message"]["content"] or "", u.get("prompt_tokens"), u.get("completion_tokens"), ch.get("finish_reason") == "length", ms)
        except (KeyError, TypeError, IndexError) as e:
            raise V3Unavailable("bad_response", f"model={model} {type(e).__name__}") from e

    def yes_prob(self, model: str, messages: list[dict], *, deadline: Deadline) -> tuple[float, Gen]:
        """'예' 일 확률 — 첫 토큰 상위 10 중 '예' 로 시작 / '아'(아니오) 로 시작하는 확률 합의 비. 생각 끔(model=base)."""
        body = {"model": model, "messages": messages, "max_tokens": 1, "temperature": 0, "logprobs": True, "top_logprobs": 10,
                "chat_template_kwargs": {"enable_thinking": False}, "skip_special_tokens": False}
        b, ms = self._post(body, deadline)
        try:
            top = b["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
            toks = [(x["token"].strip(), math.exp(x["logprob"])) for x in top]
        except (KeyError, TypeError, IndexError) as e:
            raise V3Unavailable("no_logprobs", f"model={model} {type(e).__name__}") from e
        if not toks:
            raise V3Unavailable("no_logprobs", f"model={model} empty")
        yes = sum(p for s, p in toks if s.startswith("예"))
        no = sum(p for s, p in toks if s.startswith("아"))
        p = yes / (yes + no) if yes + no else (1.0 if toks[0][0].startswith("예") else 0.0)
        u = b.get("usage") or {}
        return p, Gen("", u.get("prompt_tokens"), u.get("completion_tokens"), False, ms)

    def close(self) -> None:
        self._client.close()
