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
**태그 규격(TagSpec)** = 이름 · 축 · 시스템 프롬프트 한 묶음(10-13). 설정 `PROFILING_TAG_SPEC` 하나로 고른다 — tag4(기본, 4축) ·
tag3(카테고리 · IP · 브랜드, v2 운영 — 의사결정기록 DR-038). 축 · 프롬프트 · 스키마 · 판 이름이 함께 움직여 어긋날 수 없고,
프롬프트 파일에 적힌 축(`- 카테고리:` 꼴 줄)이 규격의 축과 다르면 load_spec 이 거부한다(서버가 뜨지 않음). 바꾸면 서버를 다시 켠다.
**시스템 프롬프트는 파일** `src/profiling/prompts/<규격>_system.md` — 문구만 바꿔 시험하려면 PROFILING_TAG_PROMPT_FILE(축은 규격을 따른다).
프롬프트를 바꾸면 판 이름(PROMPT_VERSION = <규격>-<글 + 스키마 해시 8자>)이 바뀌고 실행 기록 · 프로필의 prompt_version 에 남는다.
서비스는 기동 때 load_spec() 한 번 → app.state.tag_spec → 요청마다 pipeline.profile(tag_spec=…) 로 넘긴다. 아래 모듈 값
(SPEC · AXES · SYSTEM · SCHEMA · PROMPT_VERSION)은 기본 규격 — 도구 · 시험 편의용.
모델 비교 실험 도구도 이 모듈을 그대로 쓴다(10-12 비교 실험의 프롬프트 해시 2fc950d4.
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

ALL_AXES = ("카테고리", "IP", "브랜드", "특징")
SPEC_AXES: dict[str, tuple[str, ...]] = {          # 규격 이름 → 축. 프롬프트 파일은 prompts/<이름>_system.md
    "tag4": ALL_AXES,
    "tag3": ("카테고리", "IP", "브랜드"),           # v2 운영(DR-038) — 특징은 v2 에서 쓰는 기능이 없다
}
PROMO = re.compile(r"증정|사은품|선물포장|쇼핑백|메세지카드|택1|택일|랜덤|단독|각인|NEW|신상|한정|리뉴얼|에디션|BEST|베스트|1위|인기|누적판매|"
                   r"백화점|공식|정품|본사|당일발송|빠른배송|무료배송|추천", re.IGNORECASE)

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
DEFAULT_PROMPT_FILE = PROMPT_DIR / "tag4_system.md"   # 기본 규격 tag4 의 파일


def load_system(path: Path | str | None = None) -> str:
    """시스템 프롬프트를 파일에서 읽는다(10-13: 코드 밖으로 뺌 — 고쳐 가며 시험하기 쉽게).
    path 가 없으면 설정 TAG_PROMPT_FILE, 그것도 없으면 설정 TAG_SPEC 규격의 prompts/<규격>_system.md.
    끝 줄바꿈은 떼고 쓴다 — 같은 글이면 같은 해시(PROMPT_VERSION)."""
    if path is None:
        from profiling.settings import get_settings

        path = get_settings().TAG_PROMPT_FILE or PROMPT_DIR / f"{get_settings().TAG_SPEC}_system.md"
    text = Path(path).read_text(encoding="utf-8").rstrip("\n")
    if not text.strip():
        raise ValueError(f"태그 프롬프트 파일이 비었음: {path}")
    return text


def schema_for(axes: Sequence[str]) -> dict[str, Any]:
    """LLM 출력 스키마 — axis 는 이 축들만(Ollama format · OpenRouter json_schema 가 강제한다)."""
    return {"type": "object", "properties": {"tags": {"type": "array", "items": {
        "type": "object", "properties": {"axis": {"type": "string", "enum": list(axes)}, "tag": {"type": "string"},
                                         "ref": {"type": "string"}, "evidence": {"type": "string"}},
        "required": ["axis", "tag", "ref", "evidence"]}}}, "required": ["tags"]}


def declared_axes(system: str) -> set[str]:
    """프롬프트 글에 정의된 축 — '- 카테고리: …' 처럼 줄 머리에 축 이름이 오는 줄만(알려진 축 이름 안에서)."""
    return {m for m in re.findall(r"^- (\S+?):", system, re.MULTILINE) if m in ALL_AXES}


class TagSpec(NamedTuple):
    """태그 규격 — 이름 · 축 · 시스템 프롬프트 한 묶음. 스키마 · 해시 · 판 이름은 여기서 계산한다(어긋날 수 없게)."""

    name: str
    axes: tuple[str, ...]
    system: str
    prompt_file: Path

    @property
    def schema(self) -> dict[str, Any]:
        return schema_for(self.axes)

    @property
    def sha256(self) -> str:
        return hashlib.sha256((self.system + json.dumps(self.schema)).encode()).hexdigest()

    @property
    def version(self) -> str:
        return f"{self.name}-{self.sha256[:8]}"    # 프롬프트 글 + 스키마의 해시 — 파일을 고치면 자동으로 바뀐다

    @property
    def validator_version(self) -> str:
        return f"{self.name}-v1"                   # 검증 규칙은 같고 받는 축만 다르다 — tag4-v1 은 10-12 와 같은 값


def load_spec(name: str | None = None, prompt_file: Path | str | None = None) -> TagSpec:
    """규격 하나를 읽는다. name 이 없으면 설정 TAG_SPEC, prompt_file 이 없으면 설정 TAG_PROMPT_FILE, 그것도 없으면 prompts/<name>_system.md.
    모르는 이름 · 빈 파일 · **프롬프트에 적힌 축이 규격의 축과 다르면 ValueError** — 서버는 뜨지 않는다(조용히 다른 축으로 돌지 않게)."""
    if name is None or prompt_file is None:
        from profiling.settings import get_settings

        st = get_settings()
        name = name or st.TAG_SPEC
        prompt_file = prompt_file or st.TAG_PROMPT_FILE
    if name not in SPEC_AXES:
        raise ValueError(f"모르는 태그 규격 {name!r} — 쓸 수 있는 것: {', '.join(SPEC_AXES)}")
    path = Path(prompt_file) if prompt_file else PROMPT_DIR / f"{name}_system.md"
    system = load_system(path)
    axes = SPEC_AXES[name]
    found = declared_axes(system)
    if found != set(axes):
        raise ValueError(f"태그 규격 {name} 의 축 {'·'.join(axes)} 과 프롬프트 {path.name} 에 적힌 축 {'·'.join(sorted(found)) or '(없음)'} 이 다르다"
                         " — PROFILING_TAG_SPEC 과 PROFILING_TAG_PROMPT_FILE 을 맞춰라")
    return TagSpec(name, axes, system, path)


SPEC = load_spec()                     # 기본 규격(설정대로) — 도구 · 시험 편의용. 서비스는 기동 때 따로 읽어 요청마다 넘긴다
AXES, SYSTEM, SCHEMA, PROMPT_FILE = SPEC.axes, SPEC.system, SPEC.schema, SPEC.prompt_file
PROMPT_SHA256, PROMPT_VERSION = SPEC.sha256, SPEC.version


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


def validate(tags: Sequence[dict[str, Any]], ratings: dict[str, int], has_pref: bool,
             spec: TagSpec | None = None) -> tuple[list[dict], list[dict]]:
    """근거 ref 는 여러 개일 수 있다("pref, r1") — 하나라도 취향 문장이거나 4~5점 리뷰면 통과, 전부 1~3점이거나 없는 번호면 버림.
    축은 spec(없으면 기본 규격)의 축만 받는다 — 밖이면 '축 밖'."""
    axes = (spec or SPEC).axes
    keep, drop = [], []
    for t in tags:
        tag = str(t.get("tag", "")).strip()
        refs = [x for x in re.split(r"[\s,/·]+", str(t.get("ref", ""))) if x]
        known = [x for x in refs if (x == "pref" and has_pref) or x in ratings]
        good = [x for x in known if x == "pref" or ratings[x] >= 4]
        why = ("축 밖" if t.get("axis") not in axes else "빈 태그" if not tag else "판촉 단어" if PROMO.search(tag)
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
