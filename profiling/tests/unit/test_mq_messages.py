"""mq.messages — 요청 메시지 해석 · 엄격한 검사 (설계 §2)."""
import json

import pytest

from profiling import pipeline
from profiling.mq import messages as m

RID = "8a3851f6-1a29-4f44-a812-f6d05ad3d8e1"


def _doc(**over):
    doc = {
        "schemaVersion": 1,
        "requestId": RID,
        "createdAt": "2026-11-07T08:00:00Z",
        "payload": {
            "recipientUserId": 42,
            "sourceVersion": 7,
            "dislikedCategories": [{"categoryId": 3, "categoryName": "뷰티"}],
            "giftPreference": "커피",
            "reviews": [{"productId": 10, "rating": 5, "reviewText": "좋아요"}],
        },
    }
    for k, v in over.items():
        if k.startswith("p_"):
            doc["payload"][k[2:]] = v
        else:
            doc[k] = v
    return doc


def _b(doc) -> bytes:
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")


def test_정상_요청():
    body = _b(_doc())
    got = m.parse_request(body, RID)
    assert got.identity == m.Identity(RID, 42, 7)
    assert got.request.disliked_categories[0].category_name == "뷰티"
    assert got.input_hash == pipeline.input_hash(got.request)
    assert got.body_sha256 == m.body_sha256(body)
    assert got.trace_id is None


def test_모든_입력이_비어도_정상():
    got = m.parse_request(_b(_doc(p_dislikedCategories=[], p_giftPreference=None, p_reviews=[])), RID)
    assert got.request.reviews == [] and got.request.gift_preference is None


def test_모르는_칸은_무시():
    got = m.parse_request(_b(_doc(extra=1, p_newField="x")), RID)
    assert got.identity.request_id == RID


def test_input_hash_는_requestId_traceId_createdAt_과_무관():
    a = m.parse_request(_b(_doc()), RID)
    other = "11111111-1111-4111-8111-111111111111"
    b = m.parse_request(_b(_doc(requestId=other, traceId="t1", createdAt="2026-11-08T00:00:00Z")), other)
    assert a.input_hash == b.input_hash
    assert a.body_sha256 != b.body_sha256


def test_1000자_넘는_글도_자르지_않음():
    long = "가" * 1500
    got = m.parse_request(_b(_doc(p_giftPreference=long)), RID)
    assert got.request.gift_preference == long


@pytest.mark.parametrize("body", [b"not json", b"\xff\xfe", b"[]", b'{"requestId": "x"}'])
def test_식별_불가(body):
    with pytest.raises(m.Unidentifiable):
        m.parse_request(body, RID)


def test_JSON_중복_키는_식별_불가():
    body = ('{"schemaVersion":1,"schemaVersion":1,"requestId":"' + RID + '","createdAt":"2026-11-07T08:00:00Z",'
            '"payload":{"recipientUserId":42,"sourceVersion":7,"dislikedCategories":[],"giftPreference":null,"reviews":[]}}')
    with pytest.raises(m.Unidentifiable):
        m.parse_request(body.encode(), RID)


@pytest.mark.parametrize("mid", ["11111111-1111-4111-8111-111111111111", None, ""])
def test_message_id_가_다르거나_없으면_식별_불가(mid):
    with pytest.raises(m.Unidentifiable):
        m.parse_request(_b(_doc()), mid)


@pytest.mark.parametrize("over", [
    {"p_recipientUserId": 0}, {"p_recipientUserId": "42"}, {"p_sourceVersion": 0}, {"p_sourceVersion": True},
])
def test_수신자_버전이_잘못이면_식별_불가(over):
    with pytest.raises(m.Unidentifiable):
        m.parse_request(_b(_doc(**over)), RID)


@pytest.mark.parametrize("sv", [2, "1", True, None])
def test_규격_버전이_다르면_UNSUPPORTED(sv):
    with pytest.raises(m.Rejected) as e:
        m.parse_request(_b(_doc(schemaVersion=sv)), RID)
    assert e.value.code == m.UNSUPPORTED_SCHEMA_VERSION
    assert e.value.identity == m.Identity(RID, 42, 7)


def _drop(key):
    d = _doc()
    del d["payload"][key]
    return d


@pytest.mark.parametrize("doc", [
    _drop("dislikedCategories"), _drop("giftPreference"), _drop("reviews"),
    _doc(createdAt="2026-11-07T08:00:00"), _doc(createdAt="어제"), _doc(traceId=" "), _doc(traceId=3),
    _doc(p_dislikedCategories=[{"categoryId": i, "categoryName": "x"} for i in range(1, 7)]),
    _doc(p_dislikedCategories=[{"categoryId": "3", "categoryName": "x"}]),
    _doc(p_reviews=[{"productId": i, "rating": 5, "reviewText": None} for i in range(1, 12)]),
    _doc(p_reviews=[{"productId": 1, "rating": 6, "reviewText": None}]),
    _doc(p_reviews=[{"productId": 1, "rating": 5.0, "reviewText": None}]),
    _doc(p_reviews=[{"productId": 1, "rating": 5}]),
    _doc(p_giftPreference=3),
])
def test_입력_검증_실패는_INVALID_REQUEST(doc):
    with pytest.raises(m.Rejected) as e:
        m.parse_request(_b(doc), RID)
    assert e.value.code == m.INVALID_REQUEST
    assert e.value.identity.request_id == RID


def test_저장용_변환본은_글만_정리본으로():
    raw = "제 번호는 010-1234-5678 이고 커피를 좋아해요"
    got = m.parse_request(_b(_doc(p_giftPreference=raw, p_reviews=[{"productId": 10, "rating": 4, "reviewText": raw}])), RID)
    s = m.stored_request(got)
    assert s["requestId"] == RID and s["createdAt"] == "2026-11-07T08:00:00Z" and "traceId" not in s
    assert s["payload"]["recipientUserId"] == 42 and s["payload"]["sourceVersion"] == 7
    assert s["payload"]["dislikedCategories"] == [{"categoryId": 3, "categoryName": "뷰티"}]
    assert s["payload"]["reviews"][0]["productId"] == 10 and s["payload"]["reviews"][0]["rating"] == 4
    for text in (s["payload"]["giftPreference"], s["payload"]["reviews"][0]["reviewText"]):
        assert "010-1234-5678" not in text and "커피" in text
    assert raw not in json.dumps(s, ensure_ascii=False)


def test_빈_글은_null():
    got = m.parse_request(_b(_doc(p_giftPreference="  ", p_reviews=[{"productId": 1, "rating": 5, "reviewText": None}])), RID)
    s = m.stored_request(got)
    assert s["payload"]["giftPreference"] is None and s["payload"]["reviews"][0]["reviewText"] is None
