"""한 사람의 30개 선정 과정을 단계별로 — 입력 → 별점 편향 → 점수 순위 → 비선호 제외 → 태그별 후보 → 점수 → MMR 한 걸음씩 → 채우기.

    cd profiling && uv run python -m tools.bench.trace_selection --user U04 --tagger gemma4:12b [--mode penalty] [--steps 12]

코어 함수(category_scores · rank_products · selection 상수)를 그대로 쓰고, MMR 은 selection._embedding_pick 과 같은 식을 한 걸음씩
기록하며 다시 돈다 — 끝에 코어 select_pool 결과와 같은지 확인한다(다르면 종료 코드 1).
태그는 그 모델의 마지막 추출 기록(tools/embed_compare/out/태그추출_<모델>_*.json)을 같은 검증기로. LLM 은 부르지 않는다.
결과: 화면 + tools/bench/out/trace_<사람>_<모델>.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import sqlalchemy as sa


def main() -> int:
    from profiling import pipeline, selection, tagging
    from profiling.catalog import DbCatalogReader
    from profiling.embedding import ProductSearchEmbedding
    from profiling.settings import get_settings
    from profiling.types import DislikedCategory, ProfileRequest, Review
    from tools.embed_compare.run import DEFAULT_OUT
    from tools.scenario_callback30.run import DEFAULT_SCENARIOS, load_scenarios

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user", default="U04")
    ap.add_argument("--tagger", default="gemma4:12b")
    ap.add_argument("--mode", default="penalty", choices=("penalty", "exclude"))
    ap.add_argument("--steps", type=int, default=12)
    ap.add_argument("--embedding-dir", default=str(Path(__file__).resolve().parents[3] / "product-search"))
    a = ap.parse_args()

    u = next(x for x in load_scenarios(DEFAULT_SCENARIOS) if x.no == a.user)
    eng = sa.create_engine(get_settings().DATABASE_URL)
    catalog = DbCatalogReader(eng)
    _, products = catalog.active()
    by_id = {p.productId: p for p in products}
    rq = ProfileRequest(recipient_user_id=1, source_version=1, gift_preference=u.preference,
                        disliked_categories=[DislikedCategory(category_id=c, category_name=n) for c, n in u.disliked],
                        reviews=[Review(product_id=r["productId"], rating=r["rating"], review_text=r["reviewText"]) for r in u.reviews])
    out: dict = {"user": u.no, "alias": u.alias, "tagger": a.tagger, "mode": a.mode}
    P = lambda s: print(s)

    # 0) 입력
    P(f"\n## {u.no} {u.alias} — {u.persona}")
    P(f"0) 입력: 취향 \"{u.preference or '-'}\" · 비선호 {[n for _, n in u.disliked] or '-'}")
    for rv in u.reviews:
        p = by_id.get(rv["productId"])
        P(f"   리뷰 {rv['rating']}점 · {p.name[:30] if p else '(없는 상품)'} [{p.categoryName if p else '-'}] · {rv['reviewText'] or '(글 없음)'}")
    out["input"] = {"preference": u.preference, "disliked": [n for _, n in u.disliked], "reviews": u.reviews}

    # 1) 별점 → 소분류 편향
    scores = pipeline.category_scores(rq, products, catalog)
    cname = {p.categoryId: p.categoryName for p in products}
    P("1) 소분류 편향 = 합(별점−3) ÷ (n+2): " + " · ".join(f"{cname[s.category_id]} {s.score_sum:+d}/({s.n}+2) = {s.bias:+.2f}" for s in scores))
    out["bias"] = [{"category": cname[s.category_id], "n": s.n, "sum": s.score_sum, "bias": round(s.bias, 4)} for s in scores]

    # 2) 점수 순위(S0)
    ranked = pipeline.rank_products(rq, products, dislike_mode=pipeline.DislikeMode(a.mode), scores=scores)
    s0 = Counter(s.product.categoryName for s in ranked[:30])
    P(f"2) 점수 순위 t = 인기 + 편향 + 감점 — 후보 {len(ranked)}개. 순위 앞 30(옛 규칙 S0)이면: {dict(s0.most_common())}")
    out["s0_top30"] = dict(s0.most_common())

    # 3) 비선호 제외
    ok = [i for i, s in enumerate(ranked) if not s.disliked]
    P(f"3) 비선호 제외: {len(ranked)} → {len(ok)}개 (비선호 {len(ranked) - len(ok)}개는 30개를 못 채울 때만)")

    # 4) 태그
    logs = sorted(DEFAULT_OUT.glob(f"태그추출_{re.sub(r'[^\w.-]', '_', a.tagger)}_2*.json"))
    raw = json.loads(logs[-1].read_text())["users"][u.no]["raw"]
    prep = tagging.prepare(rq, by_id)
    keep, drop = tagging.validate(tagging.parse(raw), prep.ratings, prep.has_pref)
    tags = tagging.tags_of(tagging.to_validation(keep, drop, []))
    P(f"4) 태그({a.tagger}): " + (" · ".join(f"{t.axis}:{t.text}" for t in tags) or "없음 → 순위대로"))
    out["tags"] = [[t.axis, t.text] for t in tags]

    emb = ProductSearchEmbedding(Path(a.embedding_dir), eng)
    picked: list[int] = []
    steps: list[dict] = []
    if tags:
        V = emb.product_vectors([ranked[i].product.productId for i in ok])
        Q = emb.encode([t.text for t in tags])
        sims = V @ Q.T
        n = len(ok)
        cand: set[int] = set()
        for j, t in enumerate(tags):
            top = np.lexsort((np.arange(n), -sims[:, j]))[:selection.PER_TAG]
            cand.update(top.tolist())
            cc = Counter(ranked[ok[i]].product.categoryName for i in top)
            P(f"5) 태그 '{t.text}' 상위 {selection.PER_TAG}: {dict(cc.most_common(4))} … (최고 코사인 {sims[top[0], j]:.3f})")
        w = np.array([selection.AXIS_WEIGHT.get(t.axis, 1.0) for t in tags])
        pct = np.stack([selection._pct(sims[:, j]) for j in range(sims.shape[1])], axis=1)
        simterm = selection.SIM_WEIGHT * (pct * w).max(axis=1)
        total = np.array([ranked[i].popularity + ranked[i].bias for i in ok]) + simterm
        cl = sorted(cand)
        rel = selection._pct(total[cl])
        P(f"6) 후보 K = 태그별 상위 {selection.PER_TAG} 합집합 {len(cl)}개 · 점수 = 유사도(0~2) + 인기(0~2) + 편향 · 관련도 = 후보 안 점수 백분위")
        cv = V[cl]
        red = np.zeros(len(cl))
        left = np.ones(len(cl), dtype=bool)
        keys = {}
        per_cat: Counter = Counter()
        P(f"7) MMR 한 걸음씩: 값 = {selection.MMR_LAMBDA}×관련도 − {1 - selection.MMR_LAMBDA:.1f}×(이미 고른 것과 최대 코사인) · 한 소분류 ≤ {selection.CAT_CAP} · 같은 모델 하나")
        while left.any() and len(picked) < 30:
            mmr = np.where(left, selection.MMR_LAMBDA * rel - (1 - selection.MMR_LAMBDA) * red, -np.inf)
            j = int(np.argmax(mmr))
            left[j] = False
            i = ok[cl[j]]
            p = ranked[i].product
            key = selection.dup_key(p.name, p.brand)
            why = ("같은 모델 이미 있음" if key is not None and key in keys.values() else
                   f"소분류 상한({selection.CAT_CAP})" if per_cat[p.categoryId] >= selection.CAT_CAP else "")
            row = {"step": len(steps) + 1, "product": p.name[:34], "category": p.categoryName, "sim": round(float(simterm[cl[j]]), 3),
                   "pop": round(ranked[i].popularity, 3), "bias": round(ranked[i].bias, 3), "rel": round(float(rel[j]), 3),
                   "redundancy": round(float(red[j]), 3), "mmr": round(float(mmr[j]), 3), "taken": not why, "skip": why}
            steps.append(row)
            if why:
                continue
            picked.append(i)
            keys[i] = key
            per_cat[p.categoryId] += 1
            red = np.maximum(red, cv @ cv[j])
        for r in steps[: a.steps]:
            P(f"   {r['step']:>2}. {'✓' if r['taken'] else '✗'} {r['product']:<34} [{r['category']}] 관련도 {r['rel']:.2f} − 중복 {r['redundancy']:.2f} "
              f"→ MMR {r['mmr']:+.2f}" + (f"  (건너뜀: {r['skip']})" if r["skip"] else ""))
        skipped = [r for r in steps if r["skip"]]
        P(f"   … MMR 로 {len(picked)}개 고름 · 건너뜀 {len(skipped)}개({dict(Counter(r['skip'] for r in skipped))})")
    filled = 30 - len(picked)
    if filled > 0:
        P(f"8) 채우기: 모자란 {filled}개를 점수 순위대로(상한 · 중복 지킴)")
    sel = selection.select_pool(ranked, 30, allow_disliked=a.mode == "penalty", tags=tags, embedding=emb if tags else None)
    final = Counter(by_id[pid].categoryName for pid in sel.product_ids)
    same = [ranked[i].product.productId for i in picked] == sel.product_ids[: len(picked)]
    P(f"9) 최종 30개: {len(final)}종 {dict(final.most_common())}")
    P(f"   코어 select_pool 과 같음: {'✓' if same else '✗'}")
    out |= {"steps": steps, "picked_by_mmr": len(picked), "final": dict(final.most_common()), "final_ids": sel.product_ids,
            "final_names": [by_id[pid].name for pid in sel.product_ids], "matches_core": same}
    emb.close()
    od = Path("tools/bench/out")
    od.mkdir(parents=True, exist_ok=True)
    (od / f"trace_{u.no}_{re.sub(r'[^\w.-]', '_', a.tagger)}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
