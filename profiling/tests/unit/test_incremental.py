"""증분 태그 추출(10-29) — 근거가 그대로인 이전 태그는 다시 쓰고 바뀐 글만 LLM 에 보낸다. 스위치(INCREMENTAL_TAGS)는 기본 꺼짐.

그 바탕인 저장 근거(ref, 10-28): 리뷰 순번(r1) 대신 그 리뷰의 상품 번호 — 모델에는 지금처럼 r1 을 보이고, 판정 로그 · 검증도 r1 그대로 둔다.
저장 칸(preferred_tags · disliked_tags)으로 옮길 때만 바꾼다 — 순번은 리뷰가 하나 늘면 밀리지만 상품 번호는 그 리뷰를 계속 가리킨다.
"""

import json

from profiling import guard, incremental, pipeline, tagging
from profiling.ports import TagReply
from profiling.types import (
    DislikedCategory,
    ProfileRequest,
    ProfileTag,
    RecipientProfile,
    Review,
    preference_hash,
    profile_tags,
    to_review_records,
)
from tests.unit.test_pipeline import FakeCatalog, FakeRecipientStore, FakeStore
from tests.unit.test_tagging import FakeTagger, _p, _profile
from tests.unit.test_tagging import _rq as _tag_rq

SPEC = tagging.load_spec("tag3disliked")
PV = f"{SPEC.version}@fake-llm"
PRODUCTS = [_p(i, 1, 1000 - i, parent=2) for i in range(1, 21)]
BY_ID = {p.productId: p for p in PRODUCTS}


class SeqTagger:
    """부를 때마다 다음 답을 돌려주고 받은 프롬프트를 남긴다."""

    model = "fake-llm"

    def __init__(self, *replies) -> None:
        self.replies, self.prompts = list(replies), []

    def extract(self, system, prompt, schema) -> TagReply:
        self.prompts.append(prompt)
        return TagReply(json.dumps({"tags": self.replies.pop(0)}, ensure_ascii=False))


def _rq(sv, pref=None, reviews=(), disliked=()) -> ProfileRequest:
    return ProfileRequest(recipient_user_id=7, source_version=sv, gift_preference=pref, reviews=list(reviews),
                          disliked_categories=[DislikedCategory(category_id=i, category_name=n) for i, n in disliked])


def _prev(rq, tags, dtags=(), **kw) -> RecipientProfile:
    base = {"recipient_user_id": 7, "source_version": rq.source_version, "preferred_tags": list(tags), "disliked_tags": list(dtags),
            "disliked_categories": list(rq.disliked_categories), "gift_preference_hash": preference_hash(rq.gift_preference),
            "reviews": to_review_records(rq.reviews), "tag_status": "ok", "prompt_version": PV, "validator_version": SPEC.validator_version,
            "guard_version": guard.GUARD_VERSION}
    return RecipientProfile(**{**base, **kw})


def _run(rqs, tagger, incremental_tags=True):
    rstore = FakeRecipientStore()
    outs = [pipeline.profile(rq, catalog=FakeCatalog(PRODUCTS), store=FakeStore(), recipient_store=rstore, pool_size=10, tagger=tagger,
                             tag_spec=SPEC, incremental_tags=incremental_tags) for rq in rqs]
    return outs, rstore.get(7)


A, B = Review(product_id=3, rating=5, review_text="좋아요"), Review(product_id=5, rating=5, review_text="또 좋아요")
T_PREF = ProfileTag(axis="IP", tag="산리오", source="preference", ref="pref")
T_A = ProfileTag(axis="카테고리", tag="향수", source="review_text", ref="3")
T_BOTH = ProfileTag(axis="브랜드", tag="b", source="preference", ref="pref, 3")


def _plan(prev, rq):
    return incremental.plan(prev, rq, BY_ID, prompt_version=PV, validator_version=SPEC.validator_version)


def test_plan_falls_back_when_previous_cannot_be_trusted() -> None:
    old = _rq(1, "산리오 좋아요", [A])
    new = _rq(2, "산리오 좋아요", [A])
    assert _plan(None, new) == "no_prev"
    assert _plan(_prev(old, [T_A]), _rq(1, "산리오 좋아요", [A])) == "stale"
    assert _plan(_prev(old, [], tag_status="fail:timeout"), new) == "prev_tag_status"
    assert _plan(_prev(old, [], tag_status="empty"), new) == "prev_tag_status"
    assert _plan(_prev(old, [T_A], prompt_version="tag3disliked-00000000@fake-llm"), new) == "version"
    assert _plan(_prev(old, [T_A.model_copy(update={"ref": "r1"})]), new) == "old_ref"          # 10-28 전 행(리뷰 순번)
    assert _plan(_prev(_rq(1, "산리오 좋아요", [A], [(9, "뷰티")]), [T_A]), new) == "dislike_removed"


def test_plan_keeps_tags_whose_evidence_is_unchanged() -> None:
    """별점이 바뀐 리뷰 · 바뀐 취향 문장을 근거로 한 조각은 죽고, 하나라도 살아 있으면 태그는 남는다(살아 있는 조각만)."""
    old = _rq(1, "산리오 좋아요", [A, B])
    t_b = ProfileTag(axis="카테고리", tag="머그", source="review_text", ref="5")
    new = _rq(2, "포차코 좋아요", [Review(product_id=9, rating=4, review_text="새 글"), A, Review(product_id=5, rating=2, review_text="또 좋아요")])
    p = _plan(_prev(old, [T_PREF, T_A, T_BOTH, t_b]), new)
    assert [(t["tag"], t["ref"]) for t in p.kept] == [("향수", "3"), ("b", "3")]
    assert p.send_reviews == {9, 5} and p.send_pref and p.dropped_prev == 2 and p.needs_llm


def test_merge_dedups_and_disliked_wins() -> None:
    kept = [{"axis": "IP", "tag": "산리오", "ref": "3", "polarity": "선호"}, {"axis": "브랜드", "tag": "소니", "ref": "5", "polarity": "선호"}]
    new = [{"axis": "IP", "tag": "산 리오", "ref": "r1", "polarity": "선호"}, {"axis": "브랜드", "tag": "소니", "ref": "pref", "polarity": "비선호"}]
    assert incremental.merge(kept, new) == [{"axis": "IP", "tag": "산리오", "ref": "3, r1", "polarity": "선호"},
                                            {"axis": "브랜드", "tag": "소니", "ref": "pref", "polarity": "비선호"}]


def test_added_review_sends_only_that_review_and_keeps_old_tags() -> None:
    tagger = SeqTagger([{"axis": "IP", "tag": "산리오", "ref": "pref", "evidence": "x"}, {"axis": "카테고리", "tag": "향수", "ref": "r1", "evidence": "x"}],
                       [{"axis": "카테고리", "tag": "머그", "ref": "r1", "evidence": "x", "polarity": "선호"}])
    (_, out), row = _run([_rq(1, "산리오 좋아요", [A]), _rq(2, "산리오 좋아요", [B, A])], tagger)
    second = tagger.prompts[1]
    assert second.startswith("취향 문장(pref): (없음)\n기존 태그(바뀌지 않은 글에서 뽑음) — 선호: IP:산리오, 카테고리:향수 / 비선호: 없음\n리뷰:\nr1.")
    assert "또 좋아요" in second and "리뷰 글: 좋아요" not in second and "산리오 좋아요" not in second
    assert out.tag_status == "ok"
    assert [(t.tag, t.ref) for t in row.preferred_tags] == [("산리오", "pref"), ("향수", "3"), ("머그", "5")]


def test_nothing_changed_reuses_tags_without_llm() -> None:
    tagger = SeqTagger([{"axis": "IP", "tag": "산리오", "ref": "pref", "evidence": "x"}])
    (_, out), row = _run([_rq(1, "산리오 좋아요", [A]), _rq(2, "산리오 좋아요", [A])], tagger)
    assert len(tagger.prompts) == 1 and out.tag_status == "ok" and [t.tag for t in row.preferred_tags] == ["산리오"]


def test_switch_off_extracts_everything_again() -> None:
    tagger = SeqTagger([{"axis": "IP", "tag": "산리오", "ref": "pref", "evidence": "x"}], [{"axis": "IP", "tag": "산리오", "ref": "pref", "evidence": "x"}])
    _run([_rq(1, "산리오 좋아요", [A]), _rq(2, "산리오 좋아요", [A])], tagger, incremental_tags=False)
    assert len(tagger.prompts) == 2 and tagger.prompts[1].startswith("취향 문장(pref): 산리오 좋아요\n리뷰:")


def test_removal_leaving_no_tags_falls_back_to_full() -> None:
    """근거 리뷰가 지워져 남는 태그가 0개 — 빈 결과를 다시 쓰지 않고 전체 추출로 한 번 더 본다."""
    tagger = SeqTagger([{"axis": "카테고리", "tag": "향수", "ref": "r1", "evidence": "x"}], [{"axis": "IP", "tag": "산리오", "ref": "pref", "evidence": "x"}])
    _, row = _run([_rq(1, "산리오 좋아요", [A]), _rq(2, "산리오 좋아요", [])], tagger)
    assert len(tagger.prompts) == 2 and tagger.prompts[1].startswith("취향 문장(pref): 산리오 좋아요\n리뷰:\n(없음)")
    assert [t.tag for t in row.preferred_tags] == ["산리오"]


def test_added_disliked_category_drops_restating_tag_without_llm() -> None:
    tagger = SeqTagger([{"axis": "카테고리", "tag": "뷰티 소품", "ref": "pref", "evidence": "x", "polarity": "비선호"},
                        {"axis": "IP", "tag": "산리오", "ref": "pref", "evidence": "x", "polarity": "선호"}])
    (_, out), row = _run([_rq(1, "산리오 좋아요 뷰티는 싫어요", [A]), _rq(2, "산리오 좋아요 뷰티는 싫어요", [A], [(9, "뷰티")])], tagger)
    assert len(tagger.prompts) == 1 and row.disliked_tags == [] and [t.tag for t in row.preferred_tags] == ["산리오"]
    assert [(d.tag, d.reason) for d in out.validation.log if d.decision == "drop"] == [("뷰티 소품", "BE 비선호 대분류와 같음")]


# ---------------------------------------------------------------- 저장 근거 ref = 상품 번호(10-28)


def test_prepare_maps_ref_to_product_after_dedup_and_unknown_skip() -> None:
    """같은 상품 두 번째 · 카탈로그에 없는 상품은 빠지고 순번이 당겨진다 — 대응표도 같은 순번을 따른다."""
    by_id = {1: _p(1, 10, 5), 2: _p(2, 11, 5)}
    rq = _tag_rq(reviews=[Review(product_id=2, rating=5), Review(product_id=999, rating=5), Review(product_id=2, rating=1),
                      Review(product_id=1, rating=4)])
    prep = tagging.prepare(rq, by_id)
    assert prep.ratings == {"r1": 5, "r2": 4} and prep.products == {"r1": 2, "r2": 1}


def test_profile_tags_replaces_review_refs_with_product_ids() -> None:
    keep, drop = tagging.validate([{"axis": "IP", "tag": "산리오", "ref": "pref, r2", "polarity": "선호"},
                                   {"axis": "카테고리", "tag": "향수", "ref": "r1", "polarity": "비선호"},
                                   {"axis": "브랜드", "tag": "소니", "ref": "pref", "polarity": "비선호"}], {"r1": 1, "r2": 5}, True, SPEC)
    v = tagging.to_validation(keep, drop, [], {"r1": 31, "r2": 42})
    assert [d.ref for d in v.log] == ["pref, r2", "r1", "pref"]            # 판정 로그는 모델이 쓴 그대로
    assert profile_tags(v) == [ProfileTag(axis="IP", tag="산리오", source="preference", ref="pref, 42")]
    assert profile_tags(v, dislikes=True) == [ProfileTag(axis="카테고리", tag="향수", source="review_text", ref="31"),
                                              ProfileTag(axis="브랜드", tag="소니", source="preference", ref="pref")]


def test_profile_tags_without_map_keeps_refs() -> None:
    """대응표가 없으면(옛 결과 · 대응 밖 순번) 순번을 그대로 둔다 — 지어내지 않는다."""
    keep, drop = tagging.validate([{"axis": "카테고리", "tag": "향수", "ref": "r1"}], {"r1": 5}, False, SPEC)
    assert profile_tags(tagging.to_validation(keep, drop, [])) == [ProfileTag(axis="카테고리", tag="향수", source="review_text", ref="r1")]


def test_pipeline_stores_product_id_refs() -> None:
    """요청 → 저장까지: 두 번째 리뷰(r2)에서 뽑은 태그의 ref 가 그 리뷰의 상품 번호로 저장된다."""
    products = [_p(i, 1, 1000 - i) for i in range(1, 21)]
    tagger = FakeTagger([{"axis": "카테고리", "tag": "소분류 1", "ref": "r2", "evidence": "x"},
                         {"axis": "브랜드", "tag": "b", "ref": "pref, r1", "evidence": "x"}])
    rq = _tag_rq(pref="좋아요", reviews=[Review(product_id=7, rating=5, review_text="좋아요"), Review(product_id=3, rating=5, review_text="좋아요")])
    _, prof = _profile(rq, products, tagger=tagger)
    assert [(t.tag, t.ref) for t in prof.preferred_tags] == [("소분류 1", "3"), ("b", "pref, 7")]
