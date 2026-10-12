"""태그 추출 — 취향 문장 · 리뷰(+ 리뷰 상품의 카탈로그 정보)를 LLM 에 한 번 보내 4축 선호 태그를 받고, 코드 검증기로 거른다.

위키 멀티스텝 설계 "모델은 한 번만, 그 다음은 전부 코드"의 모델 단계. 여기는 **프롬프트 · 출력 형식 · 검증기**만 갖고, 모델 호출은
`ports.TagExtractor`(구현: llm.OllamaTagExtractor)가 한다. 그래서 단위 시험은 가짜 추출기로 돈다.

  축     카테고리 · IP · 브랜드 · 특징 (10-11 결정 — 팀 '태그추출_프롬프트_유의사항' 6절)
  입력   취향 문장(giftPreference) · 리뷰(별점 · 글) · 리뷰 상품의 이름 · 브랜드 · 대분류 > 소분류 · 종류(product_kind).
         같은 상품 중복 리뷰는 앞(최신)만, 카탈로그에 없는 상품은 뺀다(score_categories 와 같은 규칙). 비선호는 넣지 않는다 — 코드가 처리
  규칙   유의사항 1 · 2 · 3절(판촉 단어 · 증정품 · 구매 경험) + "별점 4~5점과 취향 문장만 근거"
  검증기 축이 4개 중 하나 · 근거 ref(여러 개 가능)가 실제 리뷰 · 취향 · 근거가 1~3점 리뷰뿐이면 버림 · 판촉 단어 태그는 버림 · 같은 축 같은 태그 하나

결과는 ValidationResult(기존 3축 형)로 바꿔 저장한다: 특징 → key_features, 나머지 셋 → likes. 4축 이름은 판정 로그(Decision)의
`axis4` 에 남는다. 원문(리뷰 글 · 취향 문장)은 판정 로그 evidence 에도 넣지 않는다(결정 B — 저장 금지).
프롬프트를 바꾸면 PROMPT_VERSION(내용 해시)이 바뀌고 실행 기록 · 프로필의 prompt_version 에 남는다.
도구 tools/embed_compare/extract_tags.py(로컬 모델 비교)도 이 모듈을 그대로 쓴다 — 10-12 비교 실험의 프롬프트 해시 2fc950d4.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any

from profiling.schemas import ProductRecord
from profiling.selection import Tag
from profiling.types import Decision, ProfileRequest, ValidatedTag, ValidationResult

AXES = ("카테고리", "IP", "브랜드", "특징")
PROMO = re.compile(r"증정|사은품|선물포장|쇼핑백|메세지카드|택1|택일|랜덤|단독|각인|NEW|신상|한정|리뉴얼|에디션|BEST|베스트|1위|인기|누적판매|"
                   r"백화점|공식|정품|본사|당일발송|빠른배송|무료배송|추천", re.IGNORECASE)

SYSTEM = """너는 선물 추천 서비스의 취향 태그 추출기다. 선물 받을 사람이 남긴 리뷰와 취향 문장을 읽고, 그 사람이 **좋아하는 것**을 태그로 뽑는다.

축(axis)은 네 가지 중 하나만 쓴다.
- 카테고리: 상품 종류(예: 향수, 헤드폰, 케이크, 출산 선물세트). 가능하면 리뷰 상품의 소분류나 상품 종류(kind) 수준으로.
- IP: 캐릭터 · 지식재산(예: 산리오 한교동, 카카오프렌즈 춘식이). 캐릭터는 IP 와 함께 적는다.
- 브랜드: 상품의 브랜드(예: 소니, 조말론런던). 판매처 · 백화점은 브랜드가 아니다.
- 특징: 향 · 맛 · 소재 · 기능 · 질감 같은 성질(예: 은은한 향, 노이즈캔슬링, 부드러운 소재).

규칙
1. 별점 4~5점 리뷰와 취향 문장만 근거로 쓴다. 별점 1~3점 리뷰로는 선호 태그를 만들지 않는다.
2. 상품명의 판촉 문구는 상품의 특징이 아니라 판매 조건이다. 태그로도 근거로도 쓰지 마라:
   증정 · 사은품, 단독, 선물포장 · 쇼핑백 · 메세지카드, 택1 · N종 택1 · 랜덤, 각인, NEW · 신상 · 한정 · 리뉴얼 · 에디션,
   BEST · 1위 · 인기 · 누적판매, 백화점 · 공식 · 정품 · 본사, 당일발송 · 빠른배송, 추천. 상품명 맨 앞 [ ] 머리말과 따옴표 광고 문구는 대부분 판촉이다.
3. "(+○○ 증정)", "[○○ 증정]" 같은 증정품은 본품이 아니다. 그 이름 · 종류 · 캐릭터로 태그를 만들지 마라.
4. 리뷰 글의 포장 · 배송 · 증정품 · 가격 이야기는 취향이 아니다.
5. 같은 말이 여러 축에 걸리면 더 구체적인 축 하나에만 둔다(예: 포켓몬 → IP).
6. 태그는 짧은 명사구로, 사람마다 2~6개. 근거가 없으면 빈 목록을 낸다.
7. ref 는 근거가 된 리뷰 번호(r1, r2 …) 또는 취향 문장이면 pref 다."""

SCHEMA = {"type": "object", "properties": {"tags": {"type": "array", "items": {
    "type": "object", "properties": {"axis": {"type": "string", "enum": list(AXES)}, "tag": {"type": "string"},
                                     "ref": {"type": "string"}, "evidence": {"type": "string"}},
    "required": ["axis", "tag", "ref", "evidence"]}}}, "required": ["tags"]}

PROMPT_SHA256 = hashlib.sha256((SYSTEM + json.dumps(SCHEMA)).encode()).hexdigest()
PROMPT_VERSION = f"tag4-{PROMPT_SHA256[:8]}"


def build_prompt(rq: ProfileRequest, by_id: dict[int, ProductRecord]) -> tuple[str, dict[str, int]]:
    """사람 하나의 입력 — (프롬프트, ref → 별점). 리뷰는 받은 순서(최신순) 그대로 r1, r2 …"""
    lines, ratings, seen = [], {}, set()
    for rv in rq.reviews:
        if rv.product_id in seen or rv.product_id not in by_id:
            continue
        seen.add(rv.product_id)
        p = by_id[rv.product_id]
        ref = f"r{len(ratings) + 1}"
        ratings[ref] = rv.rating
        lines.append(f"{ref}. 별점 {rv.rating}점 · 상품명: {p.name} · 브랜드: {p.brand} · 분류: {p.parentCategoryName} > {p.categoryName}"
                     f" · 종류: {p.productKind} · 리뷰 글: {rv.review_text or '(없음)'}")
    body = f"취향 문장(pref): {rq.gift_preference or '(없음)'}\n리뷰:\n" + ("\n".join(lines) if lines else "(없음)")
    return body + '\n\nJSON 으로만 답하라: {"tags": [{"axis": ..., "tag": ..., "ref": ..., "evidence": ...}]}', ratings


def validate(tags: Sequence[dict[str, Any]], ratings: dict[str, int], has_pref: bool) -> tuple[list[dict], list[dict]]:
    """근거 ref 는 여러 개일 수 있다("pref, r1") — 하나라도 취향 문장이거나 4~5점 리뷰면 통과, 전부 1~3점이거나 없는 번호면 버림."""
    keep, drop = [], []
    for t in tags:
        tag = str(t.get("tag", "")).strip()
        refs = [x for x in re.split(r"[\s,/·]+", str(t.get("ref", ""))) if x]
        known = [x for x in refs if (x == "pref" and has_pref) or x in ratings]
        good = [x for x in known if x == "pref" or ratings[x] >= 4]
        why = ("축이 4개 밖" if t.get("axis") not in AXES else "빈 태그" if not tag else "판촉 단어" if PROMO.search(tag)
               else "근거 ref 가 없음" if not known
               else "근거가 별점 1~3점 리뷰뿐(" + ",".join(f"{x}={ratings[x]}" for x in known) + ")" if not good else "")
        (drop if why else keep).append({**t, "tag": tag, **({"drop": why} if why else {})})
    uniq, seen = [], set()
    for t in keep:                                  # 같은 축 · 같은 태그는 하나만
        if (t["axis"], t["tag"]) not in seen:
            seen.add((t["axis"], t["tag"]))
            uniq.append(t)
    return uniq, drop


def parse(raw: str) -> list[dict[str, Any]]:
    """모델 원출력(JSON 문자열) → 태그 목록. 깨진 JSON 은 빈 목록(태그 없음으로 진행 — 요청을 실패시키지 않는다)."""
    return parse_report(raw)[0]


def parse_report(raw: str) -> tuple[list[dict[str, Any]], str | None]:
    """parse + 왜 비었나(로그용): None = 정상 · bad_json = JSON 아님 · no_tags_list = tags 가 목록이 아님 · skipped_items = 객체 아닌 항목 버림."""
    try:
        tags = json.loads(raw).get("tags", [])
    except (json.JSONDecodeError, AttributeError):
        return [], "bad_json"
    if not isinstance(tags, list):
        return [], "no_tags_list"
    ok = [t for t in tags if isinstance(t, dict)]
    return ok, ("skipped_items" if len(ok) < len(tags) else None)


def drop_reason_key(reason: str | None) -> str:
    """검증기 버림 사유 → 집계 키(괄호 속 리뷰 번호 · 별점은 뺀다). 예: '근거가 별점 1~3점 리뷰뿐(r1=2)' → '근거가 별점 1~3점 리뷰뿐'."""
    return (reason or "?").split("(", 1)[0]


def _source(ref: str) -> str:
    return "preference" if "pref" in ref else "review_text"


def to_validation(keep: Sequence[dict], drop: Sequence[dict], disliked_names: Sequence[str]) -> ValidationResult:
    """검증 결과 → ValidationResult. 특징 → key_features · 나머지 → likes. 판정 로그엔 원문 대신 축 · ref · 사유만."""
    likes = [ValidatedTag(tag=t["tag"], source=_source(str(t.get("ref", ""))), grade="confirmed") for t in keep if t["axis"] != "특징"]
    feats = [ValidatedTag(tag=t["tag"], source=_source(str(t.get("ref", ""))), grade="confirmed") for t in keep if t["axis"] == "특징"]
    log = [Decision(tag=t["tag"], axis="key_features" if t.get("axis") == "특징" else "likes", decision="keep", ref=str(t.get("ref", "")),
                    axis4=t["axis"]) for t in keep]
    log += [Decision(tag=str(t.get("tag", "")), axis="key_features" if t.get("axis") == "특징" else "likes", decision="drop",
                     ref=str(t.get("ref", "")), axis4=t.get("axis"), reason=t.get("drop")) for t in drop]
    return ValidationResult(likes=likes, key_features=feats, dislikes=[], preferred_tags=[t["tag"] for t in keep],
                            disliked_tags=list(disliked_names), log=log)


def tags_of(validation: ValidationResult) -> list[Tag]:
    """ValidationResult → 풀 고르기 D 의 태그. 4축 이름은 판정 로그 axis4 에서(없으면 likes = 카테고리 · key_features = 특징)."""
    axis4 = {d.tag: getattr(d, "axis4", None) for d in validation.log if d.decision == "keep"}
    out = [Tag(axis4.get(t.tag) or "카테고리", t.tag) for t in validation.likes]
    out += [Tag(axis4.get(t.tag) or "특징", t.tag) for t in validation.key_features]
    order = {t: k for k, t in enumerate(validation.preferred_tags)}
    return sorted(out, key=lambda t: order.get(t.text, len(order)))
