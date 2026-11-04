"""증분 태그 추출 — 이전 프로필의 태그 중 근거가 그대로인 것은 다시 쓰고, 바뀐 글(취향 문장 · 새 리뷰 · 바뀐 리뷰)만 LLM 에 보낸다(10-29).

설계: 팀 폴더 실험/Harness2_프로파일링/docs/16_증분추출_바뀐입력만LLM_설계와효과_2026-10-26.md · 정확도 시험 증분추출_2026-10-26(incr_acc).
스위치 settings.INCREMENTAL_TAGS(기본 꺼짐) — 꺼져 있으면 지금처럼 늘 전체 추출. 켜면 pipeline._extract_tags 가 plan() 을 먼저 부른다.

  다시 쓰기  이전 태그(preferred_tags · disliked_tags)의 근거 ref 조각마다 — 'pref' 는 취향 문장 지문이 같을 때, 상품 번호는 그 리뷰의 지문
             (review_hash = 상품 · 별점 · 글)이 같을 때 살아 있다. 조각이 하나라도 살아 있으면 태그를 남기고 ref 는 살아 있는 조각만.
             ref 가 상품 번호라 가능하다(10-28 — 리뷰 순번 r1 은 리뷰가 늘면 밀린다).
  보내기     바뀐 취향 문장 · 새로 들어오거나 지문이 바뀐 리뷰만 + 다시 쓰는 태그 요약 한 줄(tagging.kept_note). 보낼 게 없으면 LLM 을 부르지 않는다
             (리뷰 삭제 · 취향 문장 삭제 · 비선호 대분류 추가만 있을 때).
  합치기     merge — 같은 태그(띄어쓰기 · 대소문자 무시)는 하나, 극성이 다르면 비선호를 남긴다. 그 뒤 비선호 대분류 다시 말함 거르기를 전체에 다시.

전체 추출로 돌아가는 경우(plan 이 사유 글자를 돌려준다 — 로그 `tag_incr fallback=<사유>`):
  no_prev           이전 프로필 없음 · 조회 실패
  stale             이전 프로필의 번호가 이번과 같거나 큼
  prev_tag_status   이전 태그 단계가 ok · skip_no_signal 이 아님(실패 · 끔 · 빈 결과 · 입력 없음 — 다시 쓸 태그를 믿을 수 없다)
  version           프롬프트(+모델) · 검증 규칙 · 가드레일 판이 다름 — 같은 글이라도 다른 태그가 나올 수 있다
  old_ref           이전 태그 ref 에 'pref' · 상품 번호가 아닌 조각(10-28 전 행의 r1 등) 또는 ref 없음
  dislike_removed   비선호 대분류가 해제됨 — 그때 버린 '대분류 다시 말함' 태그가 되살아나야 하는데 버린 태그는 저장하지 않는다
  (pipeline) no_tags_left  LLM 을 안 불러도 되는데 남는 태그가 0개 — 전체로 한 번 더 본다(빈 결과를 다시 쓰지 않음)

판정기(LLM 직접 비선호 판정)는 아직 운영 코드에 없다(v3 실험). 붙이면 **새로 뽑은 태그만** 판정한다(다시 쓰는 태그는 이미 판정을 지났다).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import NamedTuple

from profiling import guard
from profiling.schemas import ProductRecord
from profiling.types import (
    ProfileRequest,
    RecipientProfile,
    preference_hash,
    ref_parts,
    review_hash,
)

REUSABLE_TAG_STATUS = frozenset({"ok", "skip_no_signal"})


class Plan(NamedTuple):
    """증분 추출 계획 — 다시 쓸 태그(validate 의 keep 모양 · ref 는 상품 번호) · LLM 에 보낼 리뷰 상품 · 취향 문장을 보낼지 · 버린 이전 태그 수."""

    kept: list[dict]
    send_reviews: frozenset[int]
    send_pref: bool
    dropped_prev: int

    @property
    def needs_llm(self) -> bool:
        return self.send_pref or bool(self.send_reviews)


def plan(prev: RecipientProfile | None, rq: ProfileRequest, by_id: dict[int, ProductRecord], *,
         prompt_version: str, validator_version: str) -> Plan | str:
    """이전 프로필 + 이번 요청 → Plan, 또는 전체 추출로 돌아갈 사유(글자). 모델을 부르지 않는다."""
    if prev is None:
        return "no_prev"
    if prev.source_version >= rq.source_version:
        return "stale"
    if prev.tag_status not in REUSABLE_TAG_STATUS:
        return "prev_tag_status"
    if (prev.prompt_version, prev.validator_version, prev.guard_version) != (prompt_version, validator_version, guard.GUARD_VERSION):
        return "version"
    if {c.category_id for c in prev.disliked_categories} - {c.category_id for c in rq.disliked_categories}:
        return "dislike_removed"
    old_hash = {r.product_id: r.review_hash for r in prev.reviews}
    cur: dict[int, str] = {}
    for rv in rq.reviews:                                  # tagging.prepare 와 같은 규칙 — 겹친 상품은 앞(최신)만, 카탈로그 밖은 뺀다
        if rv.product_id not in cur and rv.product_id in by_id:
            cur[rv.product_id] = review_hash(rv)
    same = {p for p, h in cur.items() if old_hash.get(p) == h}
    pref_hash = preference_hash(rq.gift_preference)
    pref_same = pref_hash == prev.gift_preference_hash
    kept: list[dict] = []
    prev_tags = [(t, "선호") for t in prev.preferred_tags] + [(t, "비선호") for t in prev.disliked_tags]
    for t, polarity in prev_tags:
        parts = ref_parts(t.ref)
        if not parts or any(x != "pref" and not x.isdigit() for x in parts):
            return "old_ref"
        alive = [x for x in parts if (x == "pref" and pref_same) or (x.isdigit() and int(x) in same)]
        if alive:
            kept.append({"axis": t.axis, "tag": t.tag, "ref": ", ".join(alive), "polarity": polarity, "evidence": ""})
    return Plan(kept, frozenset(cur.keys() - same), not pref_same and pref_hash is not None, len(prev_tags) - len(kept))


def _key(tag: str) -> str:
    return re.sub(r"\s+", "", tag).casefold()


def merge(kept: Sequence[dict], new: Sequence[dict]) -> list[dict]:
    """다시 쓰는 태그 + 새로 뽑은 태그(ref 는 둘 다 상품 번호). 같은 태그는 하나 — 극성이 같으면 근거를 합치고, 다르면 비선호 쪽을 남긴다.
    순서는 다시 쓰는 태그 먼저(이전 순서 그대로), 그다음 새 태그."""
    out: dict[str, dict] = {}
    for t in [*kept, *new]:
        k = _key(t["tag"])
        cur = out.get(k)
        if cur is None:
            out[k] = dict(t)
        elif cur.get("polarity") == t.get("polarity"):
            out[k] = {**cur, "ref": ", ".join(dict.fromkeys(ref_parts(cur.get("ref")) + ref_parts(t.get("ref"))))}
        elif t.get("polarity") == "비선호":
            out[k] = dict(t)
    return list(out.values())
