"""요청 메시지 해석 · 엄격한 검사 · 원본 바이트 해시 (설계 §2 · 체크리스트 1).

요청 = BE `ProfilingRequestMessage`
  {"schemaVersion": 1, "requestId": UUID, "createdAt": UTC Instant, "traceId"?: str, "payload": 기존 7.6 본문}

검사 규칙(설계 §2 표와 그 아래 문단 그대로):
- 본문은 UTF-8 JSON 객체. JSON 중복 키는 오류.
- 문자열 숫자 · bool 을 정수로 바꾸지 않는다. 필수 칸이 없으면 기본값으로 채우지 않는다.
- schemaVersion=1 의 알 수 없는 칸은 무시한다(값은 로그에 남기지 않는다).
- 1,000자 절단은 분석 단계에서만 한다 — 여기서는 자르지 않는다(원본 보관 · 중복 판정 전에 자르지 않음).

저장(11-08 사용자 결정): 설계의 원본 바이트(raw_body) 대신 **변환본**을 저장한다 — `stored_request()`.
  변환본 = 같은 봉투 · 같은 칸, 글(취향 문장 · 리뷰 글)만 가드레일 정리본(guard.clean)으로 바꾼 것(DR-039 와 같은 방식).
  원본 바이트 해시(body_sha256)는 원본 기준. 실패 큐 복사는 ACK 전까지 큐에 남아 있는 원본 메시지로 한다.

결과(설계 §5 · 실패 코드 초안):
- 요청 ID · 수신자 · 버전을 안전하게 읽을 수 없으면 `Unidentifiable` — 실패 결과를 만들지 않고 실패 큐 + 경보만.
- 식별은 되지만 규격이 다르면 `Rejected(UNSUPPORTED_SCHEMA_VERSION)`, 입력 검증 실패면 `Rejected(INVALID_REQUEST)`.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from profiling import guard, pipeline
from profiling.types import DislikedCategory, ProfileRequest, Review

SCHEMA_VERSION = 1
MAX_DISLIKED = 5
MAX_REVIEWS = 10
_I64_MAX = 2**63 - 1

INVALID_REQUEST = "INVALID_REQUEST"
UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"


@dataclass(frozen=True)
class Identity:
    """실패 결과를 만들 수 있을 만큼 안전하게 읽은 식별자."""

    request_id: str
    recipient_user_id: int
    source_version: int


@dataclass(frozen=True)
class ParsedRequest:
    identity: Identity
    created_at: datetime
    trace_id: str | None
    request: ProfileRequest
    input_hash: str              # 분석 입력 동일성(pipeline.input_hash 재사용) — requestId · traceId · createdAt 은 포함하지 않음
    body_sha256: str             # 원본 바이트 해시 — AI 내부 조회 · 복구용, 결과 메시지에는 넣지 않음


class Unidentifiable(Exception):
    """요청 ID · 수신자 · 버전을 안전하게 읽을 수 없음 → 실패 결과 없이 실패 큐 + 경보."""

    def __init__(self, reason: str, body_sha256: str) -> None:
        super().__init__(reason)
        self.reason, self.body_sha256 = reason, body_sha256


class Rejected(Exception):
    """식별은 되지만 처리할 수 없음 → 실패 결과(code) + 실패 큐."""

    def __init__(self, code: str, reason: str, identity: Identity, body_sha256: str) -> None:
        super().__init__(reason)
        self.code, self.reason, self.identity, self.body_sha256 = code, reason, identity, body_sha256


class _Invalid(Exception):
    pass


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _no_dup_keys(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise _Invalid(f"JSON 중복 키: {k}")
        out[k] = v
    return out


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _pos_i64(v: object, name: str) -> int:
    if not _is_int(v) or not 1 <= v <= _I64_MAX:
        raise _Invalid(f"{name}: 양의 64비트 정수가 아님")
    return v


def _str_or_null(obj: dict, key: str, where: str) -> str | None:
    if key not in obj:
        raise _Invalid(f"{where}{key}: 키 없음")
    v = obj[key]
    if v is not None and not isinstance(v, str):
        raise _Invalid(f"{where}{key}: 문자열 또는 null 이 아님")
    return v


def _identity(doc: object, message_id: str | None) -> Identity | None:
    """식별자 세 개를 엄격하게 읽는다. 하나라도 못 읽으면 None."""
    if not isinstance(doc, dict):
        return None
    rid, payload = doc.get("requestId"), doc.get("payload")
    if not isinstance(rid, str) or not isinstance(payload, dict):
        return None
    try:
        if str(UUID(rid)) != rid.lower():
            return None
    except ValueError:
        return None
    if message_id != rid:
        return None                              # message_id 는 requestId 와 일치해야 한다(없음도 불일치). 손상 message_id 를 정상 requestId 로 가장하지 않는다
    uid, sv = payload.get("recipientUserId"), payload.get("sourceVersion")
    if not (_is_int(uid) and 1 <= uid <= _I64_MAX and _is_int(sv) and 1 <= sv <= _I64_MAX):
        return None
    return Identity(rid, uid, sv)


def _created_at(v: object) -> datetime:
    if not isinstance(v, str) or not v.endswith("Z"):
        raise _Invalid("createdAt: UTC ISO 8601 Instant 가 아님")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError as e:
        raise _Invalid("createdAt: 시각 형식 오류") from e
    return dt.astimezone(UTC)


def _payload(p: dict) -> ProfileRequest:
    for key in ("dislikedCategories", "giftPreference", "reviews"):
        if key not in p:
            raise _Invalid(f"payload.{key}: 키 없음")
    cats = p["dislikedCategories"]
    if not isinstance(cats, list) or len(cats) > MAX_DISLIKED:
        raise _Invalid(f"payload.dislikedCategories: 배열이 아니거나 {MAX_DISLIKED}개 초과")
    disliked = []
    for i, c in enumerate(cats):
        if not isinstance(c, dict):
            raise _Invalid(f"payload.dislikedCategories[{i}]: 객체가 아님")
        if "categoryName" not in c or not isinstance(c["categoryName"], str):
            raise _Invalid(f"payload.dislikedCategories[{i}].categoryName: 문자열이 아님")
        disliked.append(DislikedCategory(category_id=_pos_i64(c.get("categoryId"), f"dislikedCategories[{i}].categoryId"),
                                         category_name=c["categoryName"]))
    gift = _str_or_null(p, "giftPreference", "payload.")
    revs = p["reviews"]
    if not isinstance(revs, list) or len(revs) > MAX_REVIEWS:
        raise _Invalid(f"payload.reviews: 배열이 아니거나 {MAX_REVIEWS}개 초과")
    reviews = []
    for i, r in enumerate(revs):
        if not isinstance(r, dict):
            raise _Invalid(f"payload.reviews[{i}]: 객체가 아님")
        rating = r.get("rating")
        if not _is_int(rating) or not 1 <= rating <= 5:
            raise _Invalid(f"payload.reviews[{i}].rating: 1~5 정수가 아님")
        reviews.append(Review(product_id=_pos_i64(r.get("productId"), f"reviews[{i}].productId"), rating=rating,
                              review_text=_str_or_null(r, "reviewText", f"payload.reviews[{i}].")))
    return ProfileRequest(recipient_user_id=p["recipientUserId"], source_version=p["sourceVersion"],
                          gift_preference=gift, disliked_categories=disliked, reviews=reviews)


def parse_request(body: bytes, message_id: str | None) -> ParsedRequest:
    """원본 바이트 → ParsedRequest. 실패하면 Unidentifiable 또는 Rejected."""
    digest = body_sha256(body)
    try:
        doc = json.loads(body.decode("utf-8"), object_pairs_hook=_no_dup_keys)
    except (UnicodeDecodeError, ValueError, _Invalid) as e:
        raise Unidentifiable(f"JSON 해석 실패: {type(e).__name__}", digest) from e
    ident = _identity(doc, message_id)
    if ident is None:
        raise Unidentifiable("요청 ID · 수신자 · 버전을 읽을 수 없음", digest)
    sv = doc.get("schemaVersion")
    if not _is_int(sv) or sv != SCHEMA_VERSION:
        raise Rejected(UNSUPPORTED_SCHEMA_VERSION, "지원하지 않는 schemaVersion", ident, digest)
    try:
        created = _created_at(doc.get("createdAt"))
        trace = doc.get("traceId")
        if trace is not None and (not isinstance(trace, str) or not trace.strip()):
            raise _Invalid("traceId: 공백 아닌 문자열이 아님")
        rq = _payload(doc["payload"])
    except _Invalid as e:
        raise Rejected(INVALID_REQUEST, str(e), ident, digest) from e
    return ParsedRequest(ident, created, trace, rq, pipeline.input_hash(rq), digest)


def stored_request(p: ParsedRequest) -> dict:
    """AI DB 에 저장할 요청 변환본(원본 대신). 글만 guard.clean 정리본으로 바꾸고 나머지는 받은 값 그대로."""
    rq = p.request
    return {
        "schemaVersion": SCHEMA_VERSION,
        "requestId": p.identity.request_id,
        "createdAt": p.created_at.isoformat().replace("+00:00", "Z"),
        **({"traceId": p.trace_id} if p.trace_id is not None else {}),
        "guardVersion": guard.GUARD_VERSION,
        "payload": {
            "recipientUserId": rq.recipient_user_id,
            "sourceVersion": rq.source_version,
            "dislikedCategories": [{"categoryId": c.category_id, "categoryName": c.category_name} for c in rq.disliked_categories],
            "giftPreference": guard.clean(rq.gift_preference).text,
            "reviews": [{"productId": r.product_id, "rating": r.rating, "reviewText": guard.clean(r.review_text).text}
                        for r in rq.reviews],
        },
    }
