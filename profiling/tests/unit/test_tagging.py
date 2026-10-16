"""tagging(프롬프트 · 검증기) + pipeline.profile 의 태그 단계 — 가짜 추출기로(LLM 없이)."""

import json
from datetime import UTC, datetime

import numpy as np

from profiling import pipeline, selection, tagging
from profiling.ports import TagExtractError, TagReply, TokenUsage
from profiling.schemas import ProductRecord
from profiling.types import DislikedCategory, ProfileRequest, Review, RunStatus


def _p(pid: int, cat: int, views: int, name: str = "상품", brand: str = "b", kind: str | None = "종류", parent: int = 1) -> ProductRecord:
    return ProductRecord(productId=pid, name=name, brand=brand, description=None, categoryId=cat, categoryName=f"c{cat}",
                         parentCategoryId=parent, parentCategoryName=f"대{parent}", productKind=kind, price=1000, availability="available",
                         updatedAt=datetime(2026, 10, 1, tzinfo=UTC), viewCount=views)


def _rq(pref=None, reviews=(), disliked=()) -> ProfileRequest:
    return ProfileRequest(recipient_user_id=7, source_version=1, gift_preference=pref, reviews=list(reviews),
                          disliked_categories=[DislikedCategory(category_id=i, category_name=n) for i, n in disliked])


def test_prompt_lists_reviews_in_order_dedups_and_skips_unknown() -> None:
    by_id = {1: _p(1, 10, 5, "코롱 30ML", "조말론런던", "공용향수"), 2: _p(2, 11, 5, "케이크", "빵집", "케이크")}
    rq = _rq(pref="향 좋아해요", reviews=[Review(product_id=1, rating=5, review_text="은은해요"), Review(product_id=1, rating=1),
                                        Review(product_id=999, rating=5), Review(product_id=2, rating=2)])
    prompt, ratings = tagging.build_prompt(rq, by_id)
    assert ratings == {"r1": 5, "r2": 2}                                       # 같은 상품 두 번째 · 없는 상품은 빠짐
    assert "r1. 별점 5점 · 상품명: 코롱 30ML · 브랜드: 조말론런던 · 분류: 대1 > c10 · 종류: 공용향수 · 리뷰 글: 은은해요" in prompt
    assert prompt.startswith("취향 문장(pref): 향 좋아해요")
    assert tagging.load_spec("tag4").sha256.startswith("2fc950d4")           # 10-12 로컬 모델 비교에 쓴 4축 프롬프트와 같다
    assert tagging.SPEC.name == "tag3disliked" and tagging.PROMPT_SHA256.startswith("48f77f25")   # 기본은 v2 운영 3축 + 비선호(DR-043, 프롬프트 v3)


def test_validator_keeps_multi_ref_and_drops_low_rating_promo_unknown() -> None:
    ratings = {"r1": 5, "r2": 2}
    raw = [{"axis": "카테고리", "tag": "향수", "ref": "pref, r1", "evidence": "x"},
           {"axis": "카테고리", "tag": "케이크", "ref": "r2", "evidence": "x"},          # 2점 리뷰뿐 → 버림
           {"axis": "특징", "tag": "단독 구성", "ref": "r1", "evidence": "x"},           # 판촉 단어 → 버림
           {"axis": "기타", "tag": "뭔가", "ref": "r1", "evidence": "x"},                # 축 밖 → 버림
           {"axis": "브랜드", "tag": "조말론런던", "ref": "r9", "evidence": "x"},         # 없는 번호 → 버림
           {"axis": "카테고리", "tag": "향수", "ref": "r1", "evidence": "x"}]            # 같은 축 같은 태그 → 하나
    keep, drop = tagging.validate(raw, ratings, has_pref=True)
    assert [t["tag"] for t in keep] == ["향수"] and len(drop) == 4
    assert tagging.parse("깨진 json") == [] and tagging.parse('{"tags": "x"}') == []


def test_validation_maps_axes_and_keeps_four_axis_name() -> None:
    keep = [{"axis": "IP", "tag": "춘식이", "ref": "pref"}, {"axis": "특징", "tag": "은은한 향", "ref": "r1"}]
    v = tagging.to_validation(keep, [{"axis": "카테고리", "tag": "x", "ref": "r2", "drop": "근거"}], ["뷰티"])
    assert [t.tag for t in v.likes] == ["춘식이"] and [t.tag for t in v.key_features] == ["은은한 향"]
    assert v.preferred_tags == ["춘식이", "은은한 향"] and v.disliked_category_names == ["뷰티"]
    assert tagging.tags_of(v) == [selection.Tag("IP", "춘식이"), selection.Tag("특징", "은은한 향")]
    assert all(d.evidence is None for d in v.log)                             # 근거 문장(원문 조각)은 남기지 않는다


class FakeTagger:
    model = "fake-llm"

    def __init__(self, tags=None, fail=False, raw=None) -> None:
        self.tags, self.fail, self.raw, self.calls = tags or [], fail, raw, 0

    def extract(self, system, prompt, schema) -> TagReply:
        self.calls += 1
        if isinstance(self.fail, Exception):
            raise self.fail
        if self.fail:
            raise TimeoutError("느림")
        text = self.raw if self.raw is not None else json.dumps({"tags": self.tags}, ensure_ascii=False)
        return TagReply(text, TokenUsage(input_tokens=812, output_tokens=64, total_ms=1500.0, load_ms=10.0, input_ms=300.0,
                                         output_ms=1190.0, stop="stop"))


class FakeEmbedding:
    version = "fake"

    def __init__(self, cat_of) -> None:
        self.cat_of = cat_of

    def product_vectors(self, ids):
        return np.stack([np.eye(8)[self.cat_of[i] % 8] for i in ids])

    def encode(self, texts):
        return np.stack([np.eye(8)[int(t.split()[-1]) % 8] for t in texts])


def _profile(rq, products, **kw):
    from tests.unit.test_pipeline import FakeCatalog, FakeRecipientStore, FakeStore

    rstore = FakeRecipientStore()
    out = pipeline.profile(rq, catalog=FakeCatalog(products), store=FakeStore(), recipient_store=rstore, pool_size=10, **kw)
    return out, rstore.get(rq.recipient_user_id)


def test_profile_extracts_tags_stores_them_and_drives_embedding_pool(caplog) -> None:
    products = [_p(i, 1, 1000 - i) for i in range(1, 21)] + [_p(100 + i, 3, 10 + i) for i in range(1, 11)]
    tagger = FakeTagger([{"axis": "카테고리", "tag": "소분류 3", "ref": "pref", "evidence": "비밀문장"}])
    rq = _rq(pref="비밀문장 좋아요")
    with caplog.at_level("INFO", logger="profiling.pipeline"):
        out, row = _profile(rq, products, tagger=tagger, embedding=FakeEmbedding({p.productId: p.categoryId for p in products}))
    assert out.status is RunStatus.RESULT_READY and tagger.calls == 1
    assert out.validation.preferred_tags == ["소분류 3"] and [(t.axis, t.tag, t.ref) for t in row.preferred_tags] == [("카테고리", "소분류 3", "pref")]
    assert out.prompt_version == f"{tagging.PROMPT_VERSION}@fake-llm" and out.validator_version == tagging.SPEC.validator_version
    assert out.search.query_text == "소분류 3" and any(pid > 100 for pid in out.search.product_ids)   # 조회수 낮은 태그 소분류가 들어옴
    assert any(selection.POOL_RULE_D_EMBED in r.message for r in caplog.records)
    assert not any("비밀문장" in r.message for r in caplog.records)           # 원문은 로그에 없다


def test_profile_tagger_failure_still_delivers_without_tags(caplog) -> None:
    products = [_p(i, i, 100 - i) for i in range(1, 6)]
    with caplog.at_level("WARNING", logger="profiling.pipeline"):
        out, _ = _profile(_rq(pref="아무거나"), products, tagger=FakeTagger(fail=True))
    assert out.status is RunStatus.RESULT_READY and out.validation.preferred_tags == [] and out.prompt_version is None
    assert out.search.product_ids == [1, 2, 3, 4, 5]
    assert any(r.message.startswith("tag_extract fail:unknown") and r.exc_info for r in caplog.records)   # 분류 못 한 실패는 스택까지


def test_profile_skips_tagger_when_no_text_input() -> None:
    tagger = FakeTagger([{"axis": "카테고리", "tag": "x", "ref": "pref", "evidence": "x"}])
    out, _ = _profile(_rq(), [_p(1, 1, 5)], tagger=tagger)
    assert tagger.calls == 0 and out.validation.preferred_tags == []


def _tag_logs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("tag_extract")]


def test_tag_extract_log_has_tokens_counts_and_no_raw_text(caplog) -> None:
    tagger = FakeTagger([{"axis": "IP", "tag": "비밀태그", "ref": "pref", "evidence": "비밀문장"},
                         {"axis": "브랜드", "tag": "버릴태그", "ref": "r9", "evidence": "x"}])
    with caplog.at_level("INFO", logger="profiling.pipeline"):
        _profile(_rq(pref="비밀문장 좋아요"), [_p(1, 1, 5)], tagger=tagger)
    [line] = _tag_logs(caplog)
    assert line.startswith("tag_extract ok recipient=7 source_version=1 model=fake-llm")
    for part in ("in_tok=812", "out_tok=64", "llm_ms=1500.0", "stop=stop", "parsed=2", "kept=1", "dropped=1", "axes=IP:1",
                 "drops=근거 ref 가 없음:1", "reviews=0", "pref=y"):
        assert part in line, part
    assert not any(x in line for x in ("비밀", "버릴태그"))                      # 원문 · 태그 글자는 없다
    assert any("tagger=ok" in r.getMessage() for r in caplog.records)          # 완료 로그에도 태그 상태


def test_tag_extract_classified_failure_logs_kind_without_stack(caplog) -> None:
    with caplog.at_level("WARNING", logger="profiling.pipeline"):
        out, _ = _profile(_rq(pref="x"), [_p(1, 1, 5)], tagger=FakeTagger(fail=TagExtractError("timeout", "model=fake-llm ReadTimeout")))
    assert out.status is RunStatus.RESULT_READY and out.prompt_version is None
    [rec] = [r for r in caplog.records if r.getMessage().startswith("tag_extract")]
    assert rec.getMessage().startswith("tag_extract fail:timeout") and rec.exc_info is None


def test_tag_extract_broken_json_and_all_dropped_warn(caplog) -> None:
    with caplog.at_level("INFO", logger="profiling.pipeline"):
        _profile(_rq(pref="x"), [_p(1, 1, 5)], tagger=FakeTagger(raw='{"tags": [ 잘림'))
        _profile(_rq(pref="x"), [_p(1, 1, 5)], tagger=FakeTagger([{"axis": "색", "tag": "빨강", "ref": "pref"}]))
    bad, empty = _tag_logs(caplog)
    assert bad.startswith("tag_extract bad_json") and "parse=bad_json" in bad and "out_chars=13" in bad
    assert empty.startswith("tag_extract empty") and "drops=축 밖:1" in empty
    assert all(r.levelname == "WARNING" for r in caplog.records if r.getMessage().startswith("tag_extract"))


def test_parse_report_and_drop_reason_key() -> None:
    assert tagging.parse_report("nope") == ([], "bad_json")
    assert tagging.parse_report('{"tags": 3}') == ([], "no_tags_list")
    assert tagging.parse_report('{"tags": [{"a": 1}, 2]}') == ([{"a": 1}], "skipped_items")
    assert tagging.drop_reason_key("근거가 별점 1~3점 리뷰뿐(r1=2)") == "근거가 별점 1~3점 리뷰뿐"


def test_system_prompt_comes_from_file_and_version_tracks_content(tmp_path) -> None:
    """프롬프트 파일(10-13) — 기본 규격(tag3disliked)의 파일 그대로, 다른 파일이면 글이 바뀌고 해시도 바뀐다, 빈 파일은 거부."""
    import hashlib
    import json as _json

    import pytest

    assert tagging.SPEC.prompt_file.exists() and tagging.load_system(tagging.SPEC.prompt_file) == tagging.SYSTEM
    assert tagging.PROMPT_VERSION == "tag3disliked-48f77f25"
    alt = tmp_path / "alt.md"
    alt.write_text(tagging.SYSTEM + "\n8. 욕설 · 개인정보는 태그로 만들지 않는다.\n", encoding="utf-8")
    text = tagging.load_system(alt)
    assert text.endswith("만들지 않는다.") and text != tagging.SYSTEM
    assert hashlib.sha256((text + _json.dumps(tagging.SCHEMA)).encode()).hexdigest()[:8] != "2fc950d4"
    (tmp_path / "empty.md").write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError):
        tagging.load_system(tmp_path / "empty.md")
