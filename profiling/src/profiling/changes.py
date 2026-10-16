"""바뀐 내용 요약 — 같은 수신자의 이전 프로필과 이번 프로필을 비교해 "무엇이 바뀌었나"를 한 줄로 만든다(10-15, C안).

pipeline.profile() 이 수신자 프로필을 저장하기 직전에 부르고, 결과를 로그 `profile_change …` 한 줄로 남긴다(배포 · 시험 공통).
recipient_profiles 는 사람당 최신 한 벌이라 이전 값이 덮이는데, profile_runs 에는 태그 · 바뀐 입력이 남지 않는다 — 그 빈칸을 로그로 채운다.

  입력  취향 문장  지문(gift_preference_hash)으로 new · changed · removed · same · none
        리뷰       상품 번호로 +추가 · -삭제 · ~바뀜(지문이 다름 — 그중 별점이 바뀐 것은 rating 으로 따로)
        비선호     대분류 번호로 +추가 · -해제
  결과  태그       (축, 태그) 로 +생김 · -사라짐 — **개수와 축만** 남긴다
        30개       이번 30개 중 이전에 없던 번호 개수

**글은 남기지 않는다** — 취향 문장 · 리뷰 글(원문 · 정리본)과 태그 글자는 로그에 넣지 않는다(DR-039: 로그에는 글을 남기지 않음). 번호 · 개수 · 축만.
"""

from __future__ import annotations

from collections import Counter
from typing import NamedTuple

from profiling.types import RecipientProfile


class ChangeSummary(NamedTuple):
    first: bool                      # 이전 프로필 없음(신규)
    stale: bool                      # 이전 프로필이 이번보다 같거나 새 번호 — DB 가 이번 저장을 무시한다
    prev_version: int | None
    pref: str                        # new · changed · removed · same · none
    reviews_added: list[int]
    reviews_removed: list[int]
    reviews_changed: list[int]       # 지문이 바뀐 상품(별점 또는 글)
    rating_changed: list[int]        # 그중 별점이 바뀐 상품
    dislikes_added: list[int]
    dislikes_removed: list[int]
    tags_added: Counter              # 축 → 개수
    tags_removed: Counter
    pool_new: int                    # 이번 30개 중 이전에 없던 번호
    pool_size: int
    dtags_added: int = 0             # 비선호 태그(disliked_tags) 늘어난 · 줄어든 개수
    dtags_removed: int = 0

    @property
    def input_changed(self) -> bool:
        return self.first or self.pref not in ("same", "none") or bool(
            self.reviews_added or self.reviews_removed or self.reviews_changed or self.dislikes_added or self.dislikes_removed)


def _pref(prev: str | None, new: str | None) -> str:
    if prev is None:
        return "none" if new is None else "new"
    if new is None:
        return "removed"
    return "same" if prev == new else "changed"


def summarize(prev: RecipientProfile | None, new: RecipientProfile) -> ChangeSummary:
    """이전 프로필(없으면 None) → 이번 프로필의 바뀐 내용."""
    if prev is None:
        return ChangeSummary(True, False, None, _pref(None, new.gift_preference_hash), sorted(r.product_id for r in new.reviews), [], [], [],
                             sorted(c.category_id for c in new.disliked_categories), [], Counter(t.axis for t in new.preferred_tags), Counter(),
                             len(new.recommended_product_ids), len(new.recommended_product_ids), len(new.disliked_tags), 0)
    pr = {r.product_id: r for r in prev.reviews}
    nr = {r.product_id: r for r in new.reviews}
    both = pr.keys() & nr.keys()
    pd = {c.category_id for c in prev.disliked_categories}
    nd = {c.category_id for c in new.disliked_categories}
    pt = {(t.axis, t.tag) for t in prev.preferred_tags}
    nt = {(t.axis, t.tag) for t in new.preferred_tags}
    old_pool = set(prev.recommended_product_ids)
    pdt = {(t.axis, t.tag) for t in prev.disliked_tags}
    ndt = {(t.axis, t.tag) for t in new.disliked_tags}
    return ChangeSummary(
        first=False, stale=prev.source_version >= new.source_version, prev_version=prev.source_version,
        pref=_pref(prev.gift_preference_hash, new.gift_preference_hash),
        reviews_added=sorted(nr.keys() - pr.keys()), reviews_removed=sorted(pr.keys() - nr.keys()),
        reviews_changed=sorted(p for p in both if pr[p].review_hash != nr[p].review_hash),
        rating_changed=sorted(p for p in both if pr[p].rating != nr[p].rating),
        dislikes_added=sorted(nd - pd), dislikes_removed=sorted(pd - nd),
        tags_added=Counter(a for a, _ in nt - pt), tags_removed=Counter(a for a, _ in pt - nt),
        pool_new=sum(1 for p in new.recommended_product_ids if p not in old_pool), pool_size=len(new.recommended_product_ids),
        dtags_added=len(ndt - pdt), dtags_removed=len(pdt - ndt),
    )


def _ids(xs: list[int]) -> str:
    return ",".join(map(str, xs)) or "-"


def _axes(c: Counter) -> str:
    return ",".join(f"{a}{n}" for a, n in sorted(c.items())) or "-"


def log_fields(s: ChangeSummary) -> str:
    """로그 한 줄의 뒷부분 — 번호 · 개수 · 축만(글 없음). 앞부분(recipient · source_version)은 호출자가 붙인다."""
    head = "first=yes" if s.first else f"first=no prev_version={s.prev_version}" + (" stale=yes" if s.stale else "")
    return (f"{head} input_changed={'yes' if s.input_changed else 'no'} pref={s.pref} "
            f"reviews=+{len(s.reviews_added)}/-{len(s.reviews_removed)}/~{len(s.reviews_changed)} "
            f"reviews_added={_ids(s.reviews_added)} reviews_removed={_ids(s.reviews_removed)} reviews_changed={_ids(s.reviews_changed)} "
            f"rating_changed={_ids(s.rating_changed)} dislikes_added={_ids(s.dislikes_added)} dislikes_removed={_ids(s.dislikes_removed)} "
            f"tags=+{sum(s.tags_added.values())}/-{sum(s.tags_removed.values())} tags_added_axes={_axes(s.tags_added)} "
            f"tags_removed_axes={_axes(s.tags_removed)} disliked_tags=+{s.dtags_added}/-{s.dtags_removed} pool_new={s.pool_new}/{s.pool_size}")
