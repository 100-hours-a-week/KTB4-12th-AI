"""태그 추출 — 취향 문장 · 리뷰(+ 리뷰 상품의 카탈로그 정보)를 LLM 에 한 번 보내 4축 선호 태그를 받고, 코드 검증기로 거른다.

위키 멀티스텝 설계 "모델은 한 번만, 그 다음은 전부 코드"의 모델 단계. 여기는 **프롬프트 · 출력 형식 · 검증기**만 갖고, 모델 호출은
`ports.TagExtractor`(구현: llm.OllamaTagExtractor)가 한다. 그래서 단위 시험은 가짜 추출기로 돈다.

  축     카테고리 · IP · 브랜드 · 특징 (10-11 결정 — 팀 '태그추출_프롬프트_유의사항' 6절)
  입력   취향 문장(giftPreference) · 리뷰(별점 · 글) · 리뷰 상품의 이름 · 브랜드 · 대분류 > 소분류 · 종류(product_kind).
         같은 상품 중복 리뷰는 앞(최신)만, 카탈로그에 없는 상품은 뺀다(score_categories 와 같은 규칙). 비선호는 넣지 않는다 — 코드가 처리
  규칙   유의사항 1 · 2 · 3절(판촉 단어 · 증정품 · 구매 경험) + "별점 4~5점과 취향 문장만 근거"
  가드레일 LLM 앞: guard.clean — 개인정보 · 욕설 가림, 의미 없는 글 버림, 인젝션 의심 표시 / LLM 뒤: 검증기가 guard.tag_problem 으로 욕설 · 개인정보 · 문장형 · 의미 없는 태그 버림 (10-13)
  검증기 축이 4개 중 하나 · 근거 ref(여러 개 가능)가 실제 리뷰 · 취향 · 근거가 1~3점 리뷰뿐이면 버림 · 판촉 단어 태그는 버림 · 같은 축 같은 태그 하나

결과는 ValidationResult(기존 3축 형)로 바꿔 저장한다: 특징 → key_features, 나머지 셋 → likes. 4축 이름은 판정 로그(Decision)의
`axis4` 에 남는다. 원문(리뷰 글 · 취향 문장)은 판정 로그 evidence 에도 넣지 않는다(결정 B — 저장 금지).
**시스템 프롬프트는 파일** `src/profiling/prompts/tag4_system.md`(10-13 코드 밖으로 뺌) — 다른 파일로 시험하려면 PROFILING_TAG_PROMPT_FILE.
프롬프트를 바꾸면 PROMPT_VERSION(내용 해시)이 바뀌고 실행 기록 · 프로필의 prompt_version 에 남는다.
도구 tools/embed_compare/extract_tags.py(로컬 모델 비교)도 이 모듈을 그대로 쓴다 — 10-12 비교 실험의 프롬프트 해시 2fc950d4.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

from profiling import guard
from profiling.schemas import ProductRecord
from profiling.selection import Tag
from profiling.types import Decision, ProfileRequest, ValidatedTag, ValidationResult


def _axes() -> tuple[str, ...]:
    from profiling.settings import get_settings

    return tuple(x.strip() for x in get_settings().TAG_AXES.split(",") if x.strip())


AXES = _axes()   # 기본 4축(카테고리 · IP · 브랜드 · 특징) — PROFILING_TAG_AXES 로 3축 등 시험(10-13)
PROMO = re.compile(r"증정|사은품|선물포장|쇼핑백|메세지카드|택1|택일|랜덤|단독|각인|NEW|신상|한정|리뉴얼|에디션|BEST|베스트|1위|인기|누적판매|"
                   r"백화점|공식|정품|본사|당일발송|빠른배송|무료배송|추천", re.IGNORECASE)

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
DEFAULT_PROMPT_FILE = PROMPT_DIR / "tag4_system.md"


def load_system(path: Path | str | None = None) -> str:
    """시스템 프롬프트를 파일에서 읽는다(10-13: 코드 밖으로 뺌 — 고쳐 가며 시험하기 쉽게).
    path 가 없으면 설정 TAG_PROMPT_FILE(PROFILING_TAG_PROMPT_FILE, profiling/.env 가능), 그것도 없으면 prompts/tag4_system.md.
    끝 줄바꿈은 떼고 쓴다 — 같은 글이면 같은 해시(PROMPT_VERSION)."""
    if path is None:
        from profiling.settings import get_settings

        path = get_settings().TAG_PROMPT_FILE or DEFAULT_PROMPT_FILE
    text = Path(path).read_text(encoding="utf-8").rstrip("\n")
    if not text.strip():
        raise ValueError(f"태그 프롬프트 파일이 비었음: {path}")
    return text


def _prompt_file() -> Path:
    from profiling.settings import get_settings

    return Path(get_settings().TAG_PROMPT_FILE or DEFAULT_PROMPT_FILE)


PROMPT_FILE = _prompt_file()   # 실제로 읽은 파일(기동 로그 · 기록용)
SYSTEM = load_system(PROMPT_FILE)

SCHEMA = {"type": "object", "properties": {"tags": {"type": "array", "items": {
    "type": "object", "properties": {"axis": {"type": "string", "enum": list(AXES)}, "tag": {"type": "string"},
                                     "ref": {"type": "string"}, "evidence": {"type": "string"}},
    "required": ["axis", "tag", "ref", "evidence"]}}}, "required": ["tags"]}

PROMPT_SHA256 = hashlib.sha256((SYSTEM + json.dumps(SCHEMA)).encode()).hexdigest()
PROMPT_VERSION = f"tag{len(AXES)}-{PROMPT_SHA256[:8]}"   # 프롬프트 파일 글 + 스키마의 해시 — 파일을 고치면 자동으로 바뀐다


class Prepared(NamedTuple):
    """LLM 에 보낼 준비 — 프롬프트 · ref → 별점 · 정리 뒤 취향 문장이 남았나 · 가드레일 보고(표시 · 개수, 원문 없음)."""

    prompt: str
    ratings: dict[str, int]
    has_pref: bool
    guard: dict[str, Any]


def prepare(rq: ProfileRequest, by_id: dict[int, ProductRecord]) -> Prepared:
    """사람 하나의 입력 — 취향 문장 · 리뷰 글을 **가드레일(guard.clean)로 정리한 뒤** 프롬프트를 만든다(10-13).
    리뷰는 받은 순서(최신순) 그대로 r1, r2 … 의미 없는 글은 '(없음)'으로(별점 · 상품 정보는 그대로), 개인정보 · 욕설은 가린 채로."""
    flags: Counter = Counter()
    counts: Counter = Counter()

    def take(text: str | None, where: str) -> str | None:
        r = guard.clean(text)
        counts.update(r.counts)
        for f in r.flags - {"empty"}:
            flags[f"{where}:{f}"] += 1
        return r.text

    pref = take(rq.gift_preference, "pref")
    lines, ratings, seen = [], {}, set()
    for rv in rq.reviews:
        if rv.product_id in seen or rv.product_id not in by_id:
            continue
        seen.add(rv.product_id)
        p = by_id[rv.product_id]
        ref = f"r{len(ratings) + 1}"
        ratings[ref] = rv.rating
        text = take(rv.review_text, "review")
        lines.append(f"{ref}. 별점 {rv.rating}점 · 상품명: {p.name} · 브랜드: {p.brand} · 분류: {p.parentCategoryName} > {p.categoryName}"
                     f" · 종류: {p.productKind} · 리뷰 글: {text or '(없음)'}")
    body = f"취향 문장(pref): {pref or '(없음)'}\n리뷰:\n" + ("\n".join(lines) if lines else "(없음)")
    prompt = body + '\n\nJSON 으로만 답하라: {"tags": [{"axis": ..., "tag": ..., "ref": ..., "evidence": ...}]}'
    return Prepared(prompt, ratings, pref is not None, {"version": guard.GUARD_VERSION, "flags": dict(flags), "counts": dict(counts)})


def build_prompt(rq: ProfileRequest, by_id: dict[int, ProductRecord]) -> tuple[str, dict[str, int]]:
    """(프롬프트, ref → 별점) — prepare 의 앞 두 칸(가드레일 정리 포함)."""
    prep = prepare(rq, by_id)
    return prep.prompt, prep.ratings


def worth_llm(prep: Prepared) -> bool:
    """LLM 을 부를 값이 있나 — 근거가 될 수 있는 것(정리 뒤 취향 문장 · 별점 4~5점 리뷰)이 하나도 없으면 부르지 않는다.
    규칙 1(1~3점 리뷰로는 선호 태그를 만들지 않음) 때문에 1~3점 리뷰뿐인 사람은 LLM 결과가 어차피 비어야 한다."""
    return prep.has_pref or any(r >= 4 for r in prep.ratings.values())


def validate(tags: Sequence[dict[str, Any]], ratings: dict[str, int], has_pref: bool) -> tuple[list[dict], list[dict]]:
    """근거 ref 는 여러 개일 수 있다("pref, r1") — 하나라도 취향 문장이거나 4~5점 리뷰면 통과, 전부 1~3점이거나 없는 번호면 버림."""
    keep, drop = [], []
    for t in tags:
        tag = str(t.get("tag", "")).strip()
        refs = [x for x in re.split(r"[\s,/·]+", str(t.get("ref", ""))) if x]
        known = [x for x in refs if (x == "pref" and has_pref) or x in ratings]
        good = [x for x in known if x == "pref" or ratings[x] >= 4]
        why = ("축 밖" if t.get("axis") not in AXES else "빈 태그" if not tag else "판촉 단어" if PROMO.search(tag)
               else guard.tag_problem(tag) if guard.tag_problem(tag)
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
