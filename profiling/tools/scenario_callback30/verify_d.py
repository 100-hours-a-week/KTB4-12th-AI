"""풀 고르기 D 의 독립 판정 — `profiling.selection` 을 부르지 않고 명세(코어 공유 문서 §2 ⑤~⑧)만으로 콜백 30개를 검사한다.

하네스의 재현성 검사(콜백 = make_pool)는 같은 코드를 다시 부르는 것이라 D 안의 버그를 못 잡는다. 여기는 다르게 본다.
  상수      코어 상수가 명세 값(λ 0.7 · 한 소분류 10 · 태그당 50 · 유사도 2 · 특징 ½)과 같은가 — 몰래 바뀌면 걸린다
  MMR 단계  콜백의 k 번째 상품이 그 시점 '받아들일 수 있는'(상한 · 같은 모델 · 리뷰 · 비선호) 후보 중 MMR 최대였나 — 한 걸음씩.
            상한 · 중복 제약은 고를수록 엄격해지기만 하므로, 탐욕 선택(거절된 후보는 버림)의 결과 = 매 걸음 받아들일 수 있는 후보 중 최대
  채우기    MMR 이 끝난 뒤의 상품이 점수 순서 · 상한 지킴 → 상한 없이 → (감점 모드) 비선호 순서로 정확히 이어지나
점수(인기 · 편향)는 rank_products 값을 쓴다 — 그 값은 하네스의 '점수 공식 독립 재계산' 검사가 따로 확인한다. 벡터는 임베딩 어댑터(자료원)에서.
"""

from __future__ import annotations

import re

import numpy as np

SPEC = {"MMR_LAMBDA": 0.7, "CAT_CAP": 10, "PER_TAG": 50, "SIM_WEIGHT": 2.0, "AXIS_WEIGHT": {"특징": 0.5}}
_MODEL = re.compile(r"[A-Za-z]{1,6}-?\d{2,}[A-Za-z0-9-]*")
EPS = 1e-9


def _key(p) -> tuple[str, str] | None:
    m = _MODEL.findall(p.name)
    return (p.brand, m[0].upper()) if m else None


def _pct(x: np.ndarray) -> np.ndarray:
    """순위 백분위 = 나보다 작은 값의 개수 ÷ (n − 1) — 정렬 없이 직접 센다(코어와 다른 방법)."""
    n = len(x)
    return np.array([(x < v).sum() for v in x], dtype=float) / max(n - 1, 1)


def verify(ids: list[int], ranked, tags, embedding, reviewed: set[int], allow_disliked: bool, pool: int = 30) -> list[tuple[str, bool, str]]:
    from profiling import selection

    out = []
    core = {k: getattr(selection, k) for k in SPEC}
    out.append(("D 상수 = 명세(λ 0.7 · 한 소분류 10 · 태그당 50 · 유사도 2 · 특징 ½)", core == SPEC,
                "" if core == SPEC else f"코어 {core}"))
    pos = {s.product.productId: i for i, s in enumerate(ranked)}
    E = [i for i, s in enumerate(ranked) if not s.disliked and s.product.productId not in reviewed]
    cat = [s.product.categoryId for s in ranked]
    key = [_key(s.product) for s in ranked]
    S: list[int] = []

    def acceptable(i: int, cap: bool = True) -> bool:
        if i in S or (key[i] is not None and key[i] in {key[j] for j in S}):
            return False
        return not cap or sum(cat[j] == cat[i] for j in S) < SPEC["CAT_CAP"]

    seq = [pos[x] for x in ids if x in pos]
    k = 0
    if tags and embedding is not None and E:
        V = embedding.product_vectors([ranked[i].product.productId for i in E])
        Q = embedding.encode([t.text for t in tags])
        sims = V @ Q.T
        w = np.array([SPEC["AXIS_WEIGHT"].get(t.axis, 1.0) for t in tags])
        simterm = SPEC["SIM_WEIGHT"] * np.max(np.stack([_pct(sims[:, j]) for j in range(sims.shape[1])], axis=1) * w, axis=1)
        total = np.array([ranked[i].popularity + ranked[i].bias for i in E]) + simterm
        K: list[int] = sorted({E[r] for j in range(sims.shape[1])
                               for r in sorted(range(len(E)), key=lambda r: (-sims[r, j], r))[:SPEC["PER_TAG"]]})
        loc = {i: E.index(i) for i in K}
        rel = dict(zip(K, _pct(np.array([total[loc[i]] for i in K]))))
        vec = {i: V[loc[i]] for i in K}
        bad = None
        while k < len(seq) and len(S) < pool:
            cand = [i for i in K if acceptable(i)]
            if not cand:
                break
            mmr = {i: SPEC["MMR_LAMBDA"] * rel[i] - (1 - SPEC["MMR_LAMBDA"]) * max((float(vec[i] @ vec[j]) for j in S), default=0.0)
                   for i in cand}
            best = max(mmr.values())
            got = seq[k]
            if got not in mmr or mmr[got] < best - EPS:
                bad = (k + 1, ranked[got].product.name[:24], mmr.get(got), best)
                break
            S.append(got)
            k += 1
        out.append((f"MMR 단계(독립): 고른 상품마다 그 시점 받아들일 수 있는 후보 중 MMR 최대 — {k}걸음 · 후보 {len(K)}",
                    bad is None, "" if bad is None else f"{bad[0]}번째 '{bad[1]}' MMR {bad[2]} < 최대 {bad[3]:.4f}"))
    # 채우기 — 명세 순서대로 직접 만든 꼬리와 콜백의 나머지가 같아야 한다
    want: list[int] = []
    for cap in (True, False):
        for i in E:
            if len(S) + len(want) >= pool:
                break
            if i not in want and acceptable(i, cap) and not (key[i] is not None and key[i] in {key[j] for j in want}) \
                    and (not cap or sum(cat[j] == cat[i] for j in S + want) < SPEC["CAT_CAP"]):
                want.append(i)
    if allow_disliked:
        for i, s in enumerate(ranked):
            if len(S) + len(want) >= pool:
                break
            if s.disliked and s.product.productId not in reviewed and i not in S and i not in want \
                    and not (key[i] is not None and key[i] in {key[j] for j in S + want}):
                want.append(i)
    tail = seq[k:]
    ok = tail == want
    first = next((n for n, (x, y) in enumerate(zip(tail, want)) if x != y), None)
    out.append((f"채우기 단계(독립): 점수 순서 · 상한 지킴 → 상한 없이 → 비선호 — {len(want)}개", ok,
                "" if ok else f"{k + (first or 0) + 1}번째부터 다름 · 콜백 {len(tail)}개 · 명세 {len(want)}개"))
    return out
