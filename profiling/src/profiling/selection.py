"""풀 고르기 — 점수 순위(`pipeline.rank_products`)에서 30개를 뽑는 마지막 단계. **D 방식**(10-12 결정, docs/임베딩_추천_설계안_2026-10-12.md §6).

왜 순위 앞 30개를 그대로 쓰지 않나: 리뷰에 5점이 하나라도 있으면 그 소분류가 30개를 전부 차지했다(시나리오 검증 6명 모두 30/30 —
30개는 카탈로그 전체의 1% 도 안 되는데 편향 +0.67 은 순위 1/3 크기. docs/시나리오_검증_결과_2026-10-12.md).

D 의 규칙
  1. **비선호 카테고리는 후보에서 뺀다(하드).** 비선호 카테고리 상품에 높은 별점이 있어도(예: 뷰티 비선호 + 향수 5점) 그 카테고리는
     빼고, 좋게 평가한 상품에서 나온 태그만 취향 신호로 쓴다 — 선택은 지키고 취향은 살린다. 감점만 쓰면 유사도 · 편향이 감점을 넘어
     비선호가 다시 들어왔다(실험 7~8개). 비선호는 **30개를 못 채울 때만**(PENALTY 모드) 감점 순서로 채운다 — 비선호를 상한까지 고른
     입력에도 30개가 나가게(10-11 감점 결정의 목적).
  2. **한 소분류 최대 CAT_CAP(10)개** — 10-07 "편향은 순서만, 몫은 정하지 않음"을 바꾼 사용자 결정(10-12). 상한 때문에 30개가 안 차면
     상한 없이 채운다.
  3. **같은 브랜드 + 같은 모델 코드는 하나만**(색상 · 옵션만 다른 상품). 코사인으로는 못 가른다 — SONY WH-1000XM6 색상끼리 0.91~0.96,
     다른 모델(WF 이어폰)과도 0.94(10-12 측정).
  0. **리뷰한 상품은 고르지 않는다**(10-12 사용자 결정) — 이미 받은 선물을 다시 추천하지 않는다. 그 상품의 별점 · 태그는 취향 신호로만 쓴다.
  4. (태그와 임베딩이 있을 때만) 태그마다 비선호 밖에서 코사인 상위 PER_TAG 의 합집합이 후보 → 점수 = SIM_WEIGHT × 축 가중 유사도 백분위
     + 인기 + 편향 → **MMR**(λ = MMR_LAMBDA, 관련도는 후보 안 순위 백분위)로 섞으며 고른다. 태그가 없으면 이 단계 없이 순위대로.

입력 · 출력은 순수 값이다(DB · HTTP 없음). 임베딩은 `ports.EmbeddingIndex` 모양으로만 받는다 — 없으면(None) 1~3 만 한다.
같은 입력이면 같은 결과(동점은 순위 순서, 임의성 없음).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from profiling.pipeline import ScoredProduct
    from profiling.ports import EmbeddingIndex

log = logging.getLogger(__name__)

CAT_CAP = 10            # 한 소분류 최대 개수 — K 훑기(5 · 10 · 15)에서 10: 모두 8종 이상 · 취향 적중 평균 29/30
MMR_LAMBDA = 0.7        # 관련도 비중. 0.5 는 느슨한 상품(카메라 · 키링)이 섞였다(10-12 사용자 결정 0.7)
PER_TAG = 50            # 태그 하나당 후보 수
SIM_WEIGHT = 2.0        # 유사도 백분위 × 2 — 인기(조회수 백분위 × 2)와 같은 눈금
AXIS_WEIGHT = {"특징": 0.5}   # 축 가중(없으면 1.0) — "실용적인 선물" 같은 모호한 특징 태그가 순위를 뒤집지 않게

POOL_RULE_D = "v2-d:exclude-search+cap10+dedupe"            # 태그 · 임베딩 없이(1~3)
POOL_RULE_D_EMBED = "v2-d:exclude-search+embed+mmr+cap10+dedupe"   # 4 까지
POOL_RULE_D_EMBED_FAILED = "v2-d:exclude-search+cap10+dedupe+embed-failed"   # 임베딩이 실패해 1~3 만(요청은 성공)

_MODEL = re.compile(r"[A-Za-z]{1,6}-?\d{2,}[A-Za-z0-9-]*")   # WH-1000XM6 · ULT900N


class Tag(NamedTuple):
    """선호 태그 하나 — 축(카테고리 · IP · 브랜드 · 특징)과 글자. 축은 가중에만 쓴다."""

    axis: str
    text: str


class Selection(NamedTuple):
    product_ids: list[int]
    rule: str
    query_text: str           # SearchResult.query_text — 쓴 태그를 이어 붙임(없으면 "")
    candidates: int           # 임베딩 후보 수(안 썼으면 0)
    filled_disliked: int      # 모자라서 감점 순서로 채운 비선호 상품 수
    dups_skipped: int         # 같은 모델로 보고 건너뛴 수


def dup_key(name: str, brand: str) -> tuple[str, str] | None:
    """같은 브랜드 + 같은 모델 코드 → 같은 상품의 색상 · 옵션 차이로 본다. 모델 코드가 없으면 None(묶지 않음)."""
    m = _MODEL.findall(name)
    return (brand, m[0].upper()) if m else None


def select_pool(ranked: Sequence[ScoredProduct], pool_size: int, *, allow_disliked: bool,
                tags: Sequence[Tag] = (), embedding: EmbeddingIndex | None = None,
                exclude_ids: frozenset[int] | set[int] = frozenset()) -> Selection:
    """ranked = rank_products() 결과(후보 전체, 점수 순). allow_disliked = PENALTY 모드(비선호를 채움용으로 허용).
    exclude_ids = 고르지 않을 상품(리뷰한 상품) — 비선호 채움에서도 뺀다."""
    keys = [dup_key(s.product.name, s.product.brand) for s in ranked]
    cats = [s.product.categoryId for s in ranked]
    ok = [i for i, s in enumerate(ranked) if not s.disliked and s.product.productId not in exclude_ids]
    picked: list[int] = []
    seen_keys: set[tuple[str, str]] = set()
    per_cat: dict[int, int] = {}
    skipped = 0

    def take(i: int, *, cap: bool) -> bool:
        nonlocal skipped
        if keys[i] is not None and keys[i] in seen_keys:
            skipped += 1
            return False
        if cap and per_cat.get(cats[i], 0) >= CAT_CAP:
            return False
        picked.append(i)
        per_cat[cats[i]] = per_cat.get(cats[i], 0) + 1
        if keys[i] is not None:
            seen_keys.add(keys[i])
        return True

    rule, cands, query = POOL_RULE_D, 0, ""
    if tags and embedding is not None and ok:
        try:
            cands = _embedding_pick(ranked, ok, tags, embedding, pool_size, lambda i: take(i, cap=True), picked)
            rule, query = POOL_RULE_D_EMBED, " ".join(t.text for t in tags)
        except Exception as e:  # noqa: BLE001 — 임베딩(질의 인코더 · DB 유사도)이 실패해도 요청은 실패시키지 않는다(LLM 실패와 같은 원칙)
            # 외부 호출(encode · similarities · product_vectors)은 모두 고르기 전에 끝나므로, 여기서는 아직 고른 것이 없다 — 그래도 비우고 순위대로
            log.warning("임베딩 단계 실패 — 태그 없이 순위대로 고른다(%s)", type(e).__name__)
            picked.clear()
            per_cat.clear()
            seen_keys.clear()
            rule = POOL_RULE_D_EMBED_FAILED
    for cap in (True, False):                     # 순위대로 채움 — 상한 지킴 → 그래도 모자라면 상한 없이
        for i in ok:
            if len(picked) >= pool_size:
                break
            if i not in picked:
                take(i, cap=cap)
    filled = 0
    if allow_disliked:                            # 그래도 모자라면 비선호를 감점(순위) 순서로 — 안전장치
        for i, s in enumerate(ranked):
            if len(picked) >= pool_size:
                break
            if s.disliked and s.product.productId not in exclude_ids and i not in picked and take(i, cap=False):
                filled += 1
    return Selection([ranked[i].product.productId for i in picked[:pool_size]], rule, query, cands, filled, skipped)


def _embedding_pick(ranked, ok: list[int], tags: Sequence[Tag], embedding: EmbeddingIndex, pool_size: int,
                    take: Callable[[int], bool], picked: list[int]) -> int:
    """비선호 밖(ok)에서 태그마다 상위 PER_TAG → 후보 → 점수 → MMR 로 하나씩 고른다. take(i) 가 거절하면(중복 · 상한) 건너뛰고,
    받아들인 상품만 '이미 고른 것'으로 쳐서 다음 MMR 감점에 쓴다(실험 도구와 같은 규칙). 후보 수를 돌려준다. numpy 는 이 경로에서만."""
    import numpy as np

    ids = [ranked[i].product.productId for i in ok]
    Q = embedding.encode([t.text for t in tags])                                    # (태그 수, d), 정규화
    sim_fn = getattr(embedding, "similarities", None)                               # DB 구현은 pgvector 로 계산(10-16)
    sims = sim_fn(ids, Q) if sim_fn else embedding.product_vectors(ids) @ Q.T       # (len(ok), 태그 수)
    w = np.array([AXIS_WEIGHT.get(t.axis, 1.0) for t in tags])
    n = len(ok)
    cand: set[int] = set()
    for j in range(sims.shape[1]):
        cand.update(np.lexsort((np.arange(n), -sims[:, j]))[:PER_TAG].tolist())     # 동점은 순위 순서
    pct = np.stack([_pct(sims[:, j]) for j in range(sims.shape[1])], axis=1)
    total = np.array([ranked[i].popularity + ranked[i].bias for i in ok]) + SIM_WEIGHT * (pct * w).max(axis=1)
    cl = sorted(cand)
    rel = _pct(total[cl])
    cv = embedding.product_vectors([ids[k] for k in cl])                            # MMR 감점용 — 후보 벡터만
    red = np.zeros(len(cl))
    left = np.ones(len(cl), dtype=bool)
    while left.any() and len(picked) < pool_size:
        j = int(np.argmax(np.where(left, MMR_LAMBDA * rel - (1 - MMR_LAMBDA) * red, -np.inf)))
        left[j] = False
        if take(ok[cl[j]]):
            red = np.maximum(red, cv @ cv[j])
    return len(cl)


def _pct(x):
    """순위 백분위(0~1) — 나보다 작은 값의 개수 ÷ (n − 1). pipeline._view_percentile 과 같은 정의."""
    import numpy as np

    s = np.sort(x)
    return np.searchsorted(s, x, side="left") / max(len(x) - 1, 1)
