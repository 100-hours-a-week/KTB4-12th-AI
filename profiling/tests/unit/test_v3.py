"""v3 태그 엔진(11-03) — AI 서버 엔진(engine) → 가짜 Modal(같은 프로세스에서 core.Core) → 가짜 vLLM · 가짜 Laya.
모두 httpx MockTransport 라 모델 · 네트워크 안 부름.

지키는 것: 실험과 같은 입력 꼴(나눠 뽑기 · 판정 질문) · 판정 · Laya 문턱 · 규칙 A·B·C(AI 서버 쪽) ·
AI 서버 → Modal 은 사람마다 한 번 · Modal 을 못 쓰면 그 사람은 v2 추출기로(prompt_version 끝 +v2-fallback) · 켜지는 동안의 5xx 는 기다림 ·
Modal 303(150초 넘김) 따라가기도 사람 한 명 기한 안에서만.
"""
import json
import math
import time

import httpx
import pytest

from profiling import status, tagging
from profiling.types import Review
from profiling.v3 import graph as gr
from profiling.v3 import rules
from profiling.v3 import text as tx
from profiling.v3.client import Deadline, V3Client, V3Unavailable, load_grammar
from profiling.v3.core import CONTRACT, Core
from profiling.v3.engine import V3Engine, request_of
from profiling.v3.laya import LABELS
from tests.unit.test_tagging import FakeTagger, _p, _profile, _rq

DIS = tagging.load_spec("tag3disliked")


# ---------------------------------------------------------------- 가짜들
class FakeLaya:
    """글에 든 말로 이름표를 정한다 — {말: 이름표}. 맞는 말이 없으면 '본인 비선호'."""

    def __init__(self, by_word=None, ready=True, boom=False) -> None:
        self.by_word, self.ready, self.boom, self.calls = by_word or {}, ready, boom, []

    def predict(self, xs):
        if not self.ready:
            raise RuntimeError("Laya 가 올라가지 않음")
        if self.boom:
            raise RuntimeError("메모리 부족")
        self.calls.append(xs)
        out = []
        for x in xs:
            lab = next((v for k, v in self.by_word.items() if k in x), "본인 비선호")
            p = [0.9 if label == lab else 0.025 for label in LABELS]
            out.append((p, [math.log(q / (1 - q)) for q in p]))
        return out


def _chat(text, pt=100, ct=20, finish="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}], "usage": {"prompt_tokens": pt, "completion_tokens": ct}}


def _yes(p):
    return {"choices": [{"message": {"content": "예"}, "logprobs": {"content": [{"top_logprobs": [
        {"token": "예", "logprob": math.log(p)}, {"token": "아니오", "logprob": math.log(1 - p)}]}]}}],
        "usage": {"prompt_tokens": 300, "completion_tokens": 1}}


class FakeServer:
    """버전 3: 취향 글만 · 리뷰만 요청에 각각 태그 JSON, 판정은 태그별 예 확률."""

    def __init__(self, pref_tags=(), review_tags=(), yes=None, fail=None) -> None:
        self.pref_tags, self.review_tags, self.yes, self.fail = list(pref_tags), list(review_tags), yes or {}, fail
        self.bodies = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            r = self.fail(request)
            if r is not None:
                return r
        b = json.loads(request.content)
        self.bodies.append(b)
        self.last_headers = request.headers
        user = b["messages"][-1]["content"]
        if b.get("logprobs"):
            tag = next(t for t in self.yes if f"'{t}'" in user)
            return httpx.Response(200, json=_yes(self.yes[tag]))
        tags = self.review_tags if user.startswith("취향 문장(pref): (없음)") else self.pref_tags
        return httpx.Response(200, json=_chat(json.dumps({"tags": tags}, ensure_ascii=False)))


class FakeModal:
    """Modal v3 컨테이너 흉내 — /v3/tags 를 같은 프로세스의 Core 로. fail(request) 가 응답을 주면 그것으로(꺼짐 · 5xx 흉내)."""

    def __init__(self, core: Core, fail=None) -> None:
        self.core, self.fail, self.bodies = core, fail, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            r = self.fail(request)
            if r is not None:
                return r
        h = request.headers
        assert request.url.path == "/v3/tags" and h["authorization"] == "Bearer k" and (h["modal-key"], h["modal-secret"]) == ("wk-test", "ws-test")
        b = json.loads(request.content)
        self.bodies.append(b)
        code, out = self.core.handle(b)
        return httpx.Response(code, json=out)


def _engine(server, laya=None, fallback=None, timeout_s=240.0, structured="grammar", modal_fail=None, spec=DIS):
    vllm = V3Client("http://vllm.test", structured=structured, transport=httpx.MockTransport(server), sleep=lambda s: None)
    modal = FakeModal(Core(vllm, laya or FakeLaya(), spec), fail=modal_fail)
    client = V3Client("http://v3.test", "k", proxy_key="wk-test", proxy_secret="ws-test", transport=httpx.MockTransport(modal), sleep=lambda s: None)
    eng = V3Engine(client, DIS, timeout_s=timeout_s, fallback=fallback)
    eng.modal = modal       # 시험에서 보려고
    return eng


PRODUCTS = [_p(i, 1, 1000 - i) for i in range(1, 21)] + [
    _p(31, 41, 5, "무선 이어폰", "소니", "이어폰", parent=40), _p(32, 42, 5, "헤드폰", "소니", "헤드폰", parent=40),
    _p(33, 43, 5, "향수", "조말론런던", "공용향수", parent=50)]
for p_ in PRODUCTS[-3:]:      # 대분류 이름 — 규칙 C 시험용(음향기기 ⊃ 이어폰)
    object.__setattr__(p_, "parentCategoryName", {40: "음향기기", 50: "뷰티"}[p_.parentCategoryId])
    object.__setattr__(p_, "categoryName", {41: "이어폰", 42: "헤드폰", 43: "향수"}[p_.categoryId])


# ---------------------------------------------------------------- 글 다루기
def test_answer_text_reads_after_thinking_channel() -> None:
    assert tx.answer_text('<|channel>생각<channel|>{"tags": []}') == '{"tags": []}'
    assert tx.answer_text("<|channel>생각만 하다 끝남") == "" and tx.answer_text(" 답 ") == "답"


def test_judge_question_splits_claim_and_criteria() -> None:
    q = tx.judge_question({"tag": "향수", "axis": "카테고리"})
    head, crit = q.split("\n판정 기준: ")
    assert head.endswith("'향수'(카테고리) 선물을 받기 싫다고 분명히 말했다.") and crit.startswith("'싫어', '별로'")


def test_merge_keeps_disliked_on_conflict() -> None:
    a = [{"axis": "카테고리", "tag": "향수", "ref": "pref", "polarity": "선호"}]
    b = [{"axis": "카테고리", "tag": "향 수", "ref": "r1", "polarity": "비선호"}, {"axis": "IP", "tag": "산리오", "ref": "r2", "polarity": "선호"}]
    assert [(t["tag"], t["polarity"]) for t in tx.merge(a, b)] == [("향 수", "비선호"), ("산리오", "선호")]
    same = tx.merge([{"tag": "x", "ref": "pref", "polarity": "선호"}], [{"tag": "x", "ref": "r1", "polarity": "선호"}])
    assert same[0]["ref"] == "pref, r1"


def test_laya_inputs_use_sentence_or_review() -> None:
    inp = ("취향 문장(pref): 룸메가 향수를 싫어해요. 저는 캐릭터.\n리뷰:\n"
           "r1. 별점 2점 · 상품명: a · 브랜드: b · 분류: 대 > 중 · 종류: 텀블러 · 리뷰 글: 텀블러 별로")
    assert tx.laya_v3(inp, {"tag": "향수", "ref": "pref", "evidence": ""}) == "문장: 룸메가 향수를 싫어해요.\n태그: 향수"
    assert tx.laya_v3(inp, {"tag": "텀블러", "ref": "r1"}) == "리뷰(별점 2점 · 종류 텀블러): 텀블러 별로\n태그: 텀블러"


# ---------------------------------------------------------------- 규칙
def test_rule_a_drops_disliked_without_evidence_in_text() -> None:
    keep = [{"axis": "카테고리", "tag": "꽃", "ref": "pref", "evidence": "", "polarity": "비선호"},
            {"axis": "카테고리", "tag": "향수", "ref": "pref", "evidence": "향수 싫어", "polarity": "비선호"},
            {"axis": "카테고리", "tag": "꽃", "ref": "pref", "evidence": "", "polarity": "선호"}]
    out = rules.apply(keep, "취향 문장(pref): 향수 싫어\n리뷰:\n(없음)", lambda t: t)
    assert [(t["tag"], t["polarity"]) for t in out] == [("향수", "비선호"), ("꽃", "선호")]


def test_rule_b_review_brand_to_kind() -> None:
    inp = "취향 문장(pref): (없음)\n리뷰:\nr1. 별점 1점 · 상품명: 무선 이어폰 · 브랜드: 소니 · 분류: 음향기기 > 이어폰 · 종류: 이어폰 · 리뷰 글: 이어폰은 별로예요"
    out = rules.apply([{"axis": "브랜드", "tag": "소니", "ref": "r1", "evidence": "이어폰은 별로예요", "polarity": "비선호"}], inp, lambda t: t)
    assert [(t["axis"], t["tag"]) for t in out] == [("카테고리", "이어폰")]


def test_rule_c_lifts_to_broader_word_in_evidence() -> None:
    rule_c = rules.make_rule_c(gr.build(PRODUCTS))
    t = {"axis": "카테고리", "tag": "이어폰", "ref": "pref", "evidence": "음향기기 선물은 그만", "polarity": "비선호"}
    assert rule_c(t)["tag"] == "음향기기"
    assert rule_c({**t, "evidence": "이어폰은 그만"}) == {**t, "evidence": "이어폰은 그만"}      # 태그를 근거에 그대로 쓰면 그대로


# ---------------------------------------------------------------- 모델 서버 부르기
def test_grammar_must_match_spec_schema() -> None:
    assert load_grammar(DIS.schema).startswith("basic_escape ::=")
    with pytest.raises(ValueError, match="다시 만들어야"):
        load_grammar({**DIS.schema, "required": []})


def test_client_waits_through_cold_start_5xx_then_times_out() -> None:
    seen = {"n": 0}

    def booting(request):
        seen["n"] += 1
        return httpx.Response(503) if seen["n"] < 4 else httpx.Response(200, json=_chat("ok"))

    c = V3Client("http://v3.test", transport=httpx.MockTransport(booting), sleep=lambda s: None)
    assert c.gen("base", [], max_tokens=5, enable_thinking=False, deadline=Deadline(240)).text == "ok" and seen["n"] == 4
    c = V3Client("http://v3.test", transport=httpx.MockTransport(lambda r: httpx.Response(503)), sleep=lambda s: None)
    with pytest.raises(V3Unavailable) as e:
        c.gen("base", [], max_tokens=5, enable_thinking=False, deadline=Deadline(1))
    assert e.value.kind == "http_503"


def test_client_connect_error_and_4xx_are_unavailable() -> None:
    def down(request):
        raise httpx.ConnectError("거부")

    c = V3Client("http://v3.test", transport=httpx.MockTransport(down), sleep=lambda s: None)
    with pytest.raises(V3Unavailable) as e:
        c.gen("base", [], max_tokens=5, enable_thinking=False, deadline=Deadline(240))
    assert e.value.kind == "connect"
    c = V3Client("http://v3.test", transport=httpx.MockTransport(lambda r: httpx.Response(401)), sleep=lambda s: None)
    with pytest.raises(V3Unavailable) as e:
        c.gen("base", [], max_tokens=5, enable_thinking=False, deadline=Deadline(240))
    assert e.value.kind == "http_401"


def test_client_yes_prob_and_body_shape() -> None:
    srv = FakeServer(yes={"향수": 0.8})
    c = V3Client("http://v3.test", "sekret-test-key", transport=httpx.MockTransport(srv), sleep=lambda s: None)
    p, _ = c.yes_prob("base", [{"role": "user", "content": "'향수'"}], deadline=Deadline(240))
    assert p == pytest.approx(0.8)
    assert srv.bodies[0]["top_logprobs"] == 10 and srv.bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "sekret-test-key" not in repr(c)
    bare = V3Client("http://v3.test", transport=httpx.MockTransport(srv))
    bare.gen("base", [{"role": "user", "content": "x"}], max_tokens=5, enable_thinking=False, deadline=Deadline(240))
    assert "modal-key" not in srv.last_headers and "authorization" not in srv.last_headers       # 같은 컨테이너 vLLM 엔 헤더 없음
    c.gen("base", [{"role": "user", "content": "x"}], max_tokens=5, enable_thinking=False, deadline=Deadline(240), schema=DIS.schema,
          grammar="root ::= x")
    assert srv.bodies[-1]["structured_outputs"] == {"grammar": "root ::= x"} and "response_format" not in srv.bodies[-1]
    no_lp = V3Client("http://v3.test", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_chat("예"))))
    with pytest.raises(V3Unavailable) as e:
        no_lp.yes_prob("base", [], deadline=Deadline(240))
    assert e.value.kind == "no_logprobs"


# ---------------------------------------------------------------- 엔진 — 버전 3
REVIEWS = [Review(product_id=31, rating=5, review_text="소리 좋아요"), Review(product_id=33, rating=2, review_text="향수 별로예요")]


def test_v3_split_extract_judge_and_laya_veto() -> None:
    srv = FakeServer(
        pref_tags=[{"axis": "IP", "tag": "산리오", "ref": "pref", "evidence": "산리오 좋아요"},
                   {"axis": "카테고리", "tag": "꽃", "ref": "pref", "evidence": "룸메가 꽃을 싫어해요", "polarity": "비선호"},
                   {"axis": "카테고리", "tag": "텀블러", "ref": "pref", "evidence": "텀블러는 많아요", "polarity": "비선호"}],
        review_tags=[{"axis": "카테고리", "tag": "향수", "ref": "r2", "evidence": "향수 별로예요", "polarity": "비선호"}],
        yes={"꽃": 0.9, "텀블러": 0.05, "향수": 0.7})
    laya = FakeLaya({"룸메": "남 얘기"})
    eng = _engine(srv, laya)
    rq = _rq(pref="산리오 좋아요. 룸메가 꽃을 싫어해요. 텀블러는 많아요.", reviews=REVIEWS)
    run = eng.run(rq, {p.productId: p for p in PRODUCTS}, PRODUCTS, catalog_key="c1")
    assert sorted((t["tag"], t["polarity"]) for t in run.keep) == [("산리오", "선호"), ("향수", "비선호")]   # 꽃 = Laya 거부 · 텀블러 = 판정 0.05
    extracts = [b for b in srv.bodies if not b.get("logprobs")]
    assert len(extracts) == 2 and all(b["structured_outputs"]["grammar"] == eng.modal.core.grammar for b in extracts)
    assert extracts[0]["messages"][0]["content"] == DIS.system and extracts[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert extracts[0]["messages"][1]["content"].endswith("리뷰:\n(없음)" + '\n\nJSON 으로만 답하라: {"tags": [{"axis": ..., "tag": ..., "ref": ..., "evidence": ...}]}')
    assert extracts[1]["messages"][1]["content"].startswith("취향 문장(pref): (없음)\n리뷰:\nr1. 별점 5점")
    judge = next(b for b in srv.bodies if b.get("logprobs"))["messages"]
    assert judge[0]["content"] == tx.JUDGE_SYSTEM and judge[1]["content"].startswith("글:\n취향 문장(pref): ") and "JSON 으로만" not in judge[1]["content"]
    assert run.calls == 5 and run.usage.input_tokens == 2 * 100 + 3 * 300
    assert len(eng.modal.bodies) == 1                                                 # AI 서버 → Modal 은 사람마다 한 번


# ---------------------------------------------------------------- pipeline — v3 · 폴백
def _rq_one():
    return _rq(pref="산리오 좋아요", reviews=[Review(product_id=3, rating=5, review_text="좋아요")])


def test_pipeline_v3_records_engine_and_prompt_version() -> None:
    status.LLM.reset()
    eng = _engine(FakeServer(pref_tags=[{"axis": "카테고리", "tag": "소분류 1", "ref": "pref", "evidence": "산리오 좋아요"}]))
    out, row = _profile(_rq_one(), PRODUCTS, tagger=eng, tag_spec=DIS)
    assert out.tag_status == "ok" and out.prompt_version == f"{DIS.version}+v3-v3@modal"
    assert [t.tag for t in row.preferred_tags] == ["소분류 1"]
    assert status.LLM.snapshot()["engine"]["v3"] == 1


def _down(request):
    raise httpx.ConnectError("x")


@pytest.mark.parametrize("why", ["modal_down", "modal_500", "vllm_down", "laya_not_ready", "laya_boom", "spec_mismatch"])
def test_pipeline_falls_back_to_v2_tagger(why, caplog) -> None:
    status.LLM.reset()
    modal_fail = {"modal_down": _down, "modal_500": lambda r: httpx.Response(500)}.get(why)
    laya = FakeLaya(ready=why != "laya_not_ready", boom=why == "laya_boom")
    srv = FakeServer(pref_tags=[{"axis": "카테고리", "tag": "꽃", "ref": "pref", "evidence": "꽃 싫어", "polarity": "비선호"}], yes={"꽃": 0.9},
                     fail=_down if why == "vllm_down" else None)
    other = DIS._replace(system=DIS.system + " ") if why == "spec_mismatch" else DIS
    v2 = FakeTagger([{"axis": "카테고리", "tag": "소분류 1", "ref": "pref", "evidence": "x"}])
    with caplog.at_level("WARNING", logger="profiling.pipeline"):
        eng = _engine(srv, laya, fallback=v2, modal_fail=modal_fail, spec=other)
        out, row = _profile(_rq(pref="꽃 싫어 산리오 좋아요"), PRODUCTS, tagger=eng, tag_spec=DIS)
    assert v2.calls == 1 and out.prompt_version == f"{DIS.version}@fake-llm+v2-fallback" and out.tag_status == "ok"
    assert [t.tag for t in row.preferred_tags] == ["소분류 1"]
    assert "v3_unavailable" in caplog.text and "꽃 싫어" not in caplog.text       # 원문은 로그에 안 남김
    assert status.LLM.snapshot()["engine"]["v2-fallback"] == 1


def test_pipeline_v3_without_fallback_continues_without_tags() -> None:
    eng = _engine(FakeServer(fail=lambda r: httpx.Response(500)), timeout_s=0.01)
    out, row = _profile(_rq_one(), PRODUCTS, tagger=eng, tag_spec=DIS)
    assert out.tag_status == "fail:v3_unavailable" and out.prompt_version is None and not row.preferred_tags
    assert out.search is not None and out.search.product_ids                         # 태그 없이도 30개(여기선 10개) 고름


def test_pipeline_v3_skips_model_when_no_signal() -> None:
    srv = FakeServer()
    out, _ = _profile(_rq(reviews=[Review(product_id=3, rating=2, review_text="별로")]), PRODUCTS, tagger=_engine(srv), tag_spec=DIS)
    assert out.tag_status == "skip_no_signal" and srv.bodies == []


def test_engine_rejects_non_disliked_spec() -> None:
    with pytest.raises(ValueError):
        V3Engine(V3Client("http://v3.test"), tagging.load_spec("tag3"))
    with pytest.raises(ValueError):
        Core(V3Client("http://vllm.test"), FakeLaya(), tagging.load_spec("tag3"))


# ---------------------------------------------------------------- AI 서버 ↔ Modal 계약
def test_request_carries_guarded_text_and_parts() -> None:
    rq = _rq(pref="산리오 좋아요. 010-1234-5678 로 연락", reviews=REVIEWS)
    by_id = {p.productId: p for p in PRODUCTS}
    b = request_of(rq, by_id, DIS, 235.0)
    assert (b["contract"], b["spec"], b["version"], b["timeout_s"]) == (CONTRACT, DIS.version, "v3", 235.0)
    assert b["text"].startswith("취향 문장(pref): ") and "JSON 으로만" not in b["text"] and "010-1234-5678" not in b["text"]
    assert [x["has_pref"] for x in b["parts"]] == [True, False] and all("010-1234-5678" not in x["prompt"] for x in b["parts"])


@pytest.mark.parametrize(("req", "err"), [({"contract": 99}, "contract_mismatch"), ({"spec": "x"}, "spec_mismatch"),
                                         ({"version": "v25"}, "bad_version"), ({"version": None}, "bad_version"),
                                         ({"text": None}, "bad_request")])
def test_core_rejects_bad_requests(req, err) -> None:
    core = Core(V3Client("http://vllm.test", transport=httpx.MockTransport(FakeServer())), FakeLaya(), DIS)
    base = {"contract": CONTRACT, "spec": DIS.version, "version": "v3", "text": "취향 문장(pref): x", "parts": []}   # 버전 25 는 11-04 버림 → bad_version
    code, out = core.handle({**base, **req})
    assert code == 409 and out == {"error": err}


def test_engine_rejects_bad_modal_response() -> None:
    def odd(request):
        return httpx.Response(200, json={"contract": CONTRACT, "tags": "깨짐"})

    eng = _engine(FakeServer(), modal_fail=odd)
    with pytest.raises(V3Unavailable) as e:
        eng.run(_rq_one(), {p.productId: p for p in PRODUCTS}, PRODUCTS)
    assert e.value.kind == "bad_response"


def test_settings_default_is_v3() -> None:
    from profiling.settings import Settings
    s = Settings(_env_file=None, DATABASE_URL="postgresql://x")
    assert s.TAG_ENGINE == "v3" and not s.V3_URL and s.V3_TIMEOUT_S < s.RUNNING_STALE_S and s.V3_FALLBACK


@pytest.mark.parametrize(("env", "on"), [({}, True), ({"V3_PROXY_SECRET": None}, False), ({"V3_PROXY_KEY": None}, False), ({"V3_API_KEY": None}, False),
                                       ({"V3_URL": ""}, False)])
def test_main_turns_v3_on_only_with_url_and_three_keys(env, on, caplog) -> None:
    from profiling.main import _v3_engine
    from profiling.settings import Settings
    kw = {"TAG_ENGINE": "v3", "V3_URL": "http://v3.test", "V3_API_KEY": "k", "V3_PROXY_KEY": "wk-test", "V3_PROXY_SECRET": "ws-test", **env}
    s = Settings(_env_file=None, DATABASE_URL="postgresql://x", **{k: v for k, v in kw.items() if v is not None})
    v2 = FakeTagger([])
    with caplog.at_level("INFO", logger="profiling.main"):
        tagger, spec = _v3_engine(s, v2, DIS)
    assert isinstance(tagger, V3Engine) is on and spec == DIS and (tagger.fallback is v2 if on else tagger is v2)
    assert "wk-test" not in caplog.text and "ws-test" not in caplog.text            # 키는 로그에 안 남김
    if on:
        tagger.close()


@pytest.mark.parametrize(("tagger_timeout", "warned"), [(30.0, False), (120.0, True)])
def test_main_warns_when_v3_plus_fallback_exceeds_stale(tagger_timeout, warned, caplog) -> None:
    from profiling.main import _v3_engine
    from profiling.settings import Settings
    s = Settings(_env_file=None, DATABASE_URL="postgresql://x", TAG_ENGINE="v3", V3_URL="http://v3.test", V3_API_KEY="k",
                 V3_PROXY_KEY="wk-test", V3_PROXY_SECRET="ws-test", TAGGER_TIMEOUT_S=tagger_timeout)
    with caplog.at_level("WARNING", logger="profiling.main"):
        tagger, _ = _v3_engine(s, FakeTagger([]), DIS)
    assert ("RUNNING_STALE_S" in caplog.text) is warned       # develop 은 30(240 + 30 < 300) — 코드 기본 120 이면 경고
    tagger.close()


def test_bind_generation_checks_before_modal_and_forwards_to_fallback() -> None:
    class Bindable(FakeTagger):
        bound = None

        def bind_generation(self, check):
            self.bound = check

    v2 = Bindable([])
    eng = _engine(FakeServer(pref_tags=[{"axis": "IP", "tag": "산리오", "ref": "pref", "evidence": "산리오 좋아요"}]), fallback=v2)
    state = {"ok": True, "n": 0}

    def check():
        state["n"] += 1
        if not state["ok"]:
            raise RuntimeError("판 바뀜")

    eng.bind_generation(check)
    assert v2.bound is check
    eng.run(_rq_one(), {p.productId: p for p in PRODUCTS}, PRODUCTS)
    assert state["n"] == 1 and len(eng.modal.bodies) == 1
    state["ok"] = False
    with pytest.raises(V3Unavailable) as e:
        eng.run(_rq_one(), {p.productId: p for p in PRODUCTS}, PRODUCTS)
    assert e.value.kind == "generation_changed" and len(eng.modal.bodies) == 1     # 판이 바뀌면 Modal 에 안 보냄


# ---------------------------------------------------------------- Modal 303 따라가기(11-03)
REDIRECT_URL = "https://v3.example.invalid"


def _redirect_client(handler) -> V3Client:
    return V3Client(REDIRECT_URL, "k", proxy_key="wk", proxy_secret="ws", transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_redirect_is_followed_as_get_with_auth_headers_and_remaining_deadline():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.headers.get("authorization"), req.headers.get("modal-key"), req.extensions["timeout"]["read"]))
        if req.method == "POST":
            time.sleep(0.05)
            return httpx.Response(303, headers={"location": f"{REDIRECT_URL}/v3/tags?__modal_function_call_id=fc-1"})
        return httpx.Response(200, json={"contract": 1, "tags": []})

    body, _ = _redirect_client(handler).post_json("/v3/tags", {"x": 1}, Deadline(10.0))
    assert body == {"contract": 1, "tags": []}
    assert [s[0] for s in seen] == ["POST", "GET"]
    assert all(s[1] == "Bearer k" and s[2] == "wk" for s in seen)          # 같은 주소 — 키 헤더가 그대로
    assert seen[1][3] < seen[0][3]                                          # 두 번째 기다림은 남은 기한만큼


def test_redirect_after_deadline_is_a_timeout_not_a_fresh_wait():
    def handler(req: httpx.Request) -> httpx.Response:
        time.sleep(0.06)
        return httpx.Response(303, headers={"location": f"{REDIRECT_URL}/v3/tags?__modal_function_call_id=fc-1"})

    with pytest.raises(V3Unavailable) as e:
        _redirect_client(handler).post_json("/v3/tags", {}, Deadline(0.05))
    assert e.value.kind == "timeout"


def test_endless_redirects_stop():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(303, headers={"location": f"{REDIRECT_URL}/v3/tags?__modal_function_call_id=fc-1"})

    with pytest.raises(V3Unavailable) as e:
        _redirect_client(handler).post_json("/v3/tags", {}, Deadline(10.0))
    assert e.value.kind == "timeout"
