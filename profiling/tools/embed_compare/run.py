"""임베딩 후보 + 코드 점수 비교 실험 — 지금 점수식(S0)과 임베딩 세 방식(A · B · C)의 30개를 같은 사람·같은 카탈로그로 나란히 본다.

    cd profiling && uv run python -m tools.embed_compare.run [--modes penalty,exclude] [--top 5]

입력은 30개 콜백 검증과 같은 시나리오 엑셀(tools/scenario_callback30/scenarios.xlsx)에 '태그' 시트를 더한 것이다(4축 양의 태그, 손으로 적음).
상품 벡터는 product-search 가 만든 것(product-search/data/vectors.f32 · manifest.json, Jina v5 nano q4f16 768차원)을 읽기만 하고,
질의 벡터는 같은 모델의 Node 실행기(product-search/embedding/service.mjs)로 만든다. 서버·LLM·유료 API 없음, DB 는 카탈로그 읽기만.

방식
  S0  지금 코드 — pipeline.rank_products(인기 백분위 × 2 + 편향 + 비선호 감점) 앞 30
  A   임베딩만 — 태그마다 코사인 상위 PER_TAG 개 → RRF 병합 순서(감점 모드에선 비선호를 뒤로)
  B   임베딩 후보 + 코드 점수 — A 의 후보 합집합을 SIM_WEIGHT × 유사도 백분위 + S0 점수(인기 + 편향 + 감점)로 정렬
  C   B + MMR — 관련도(B 점수 0~1)와 이미 고른 상품과의 유사도를 λ 로 섞어 하나씩 고른다
  D   (2차, 10-12) 비선호를 **빼고** 검색 · 특징 태그 가중 ½ · MMR 관련도는 순위 백분위 · λ 0.7 · 한 소분류 최대 10 ·
      같은 브랜드 + 같은 모델 코드는 하나만 · 30개가 안 차면 그때만 비선호를 감점 순서로 채움
태그가 없는 사람은 S0~C 가 S0 와 같다(대체). D 는 태그가 없어도 비선호 배제 · 이름 중복 제거를 한다. 후보가 30개보다 적으면 S0 순서로 채운다.

결과: 화면(사람 × 모드 × 방식 지표 · 상위 N) + tools/embed_compare/out/임베딩_비교_<시각>.xlsx · .json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import select
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Self

import numpy as np
import sqlalchemy as sa

from profiling import selection

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SEARCH = REPO / "product-search"
DEFAULT_SCENARIOS = HERE.parent / "scenario_callback30" / "scenarios.xlsx"
DEFAULT_OUT = HERE / "out"
MODES = ("penalty", "exclude")
STRATEGIES = ("S0", "A", "B", "C", "D")
LABEL = {"S0": "S0 지금 점수식", "A": "A 임베딩만", "B": "B 임베딩+점수", "C": "C B+MMR", "D": "D 배제후검색"}

POOL = 30
PER_TAG = 50          # 태그 하나당 후보 수
RRF_K = 30            # product-search 와 같은 RRF 상수
SIM_WEIGHT = 2.0      # 유사도 백분위 × 2 — 인기(백분위 × 2)와 같은 크기
MMR_LAMBDA = 0.7      # 1 이면 B 와 같고, 낮을수록 서로 다른 상품을 고른다
NEAR_DUP = 0.95       # 앞서 고른 상품과 코사인이 이 이상이면 근접 중복(같은 상품의 옵션 · 용량 차이 등)
# D 의 상수(λ 0.7 · 한 소분류 최대 10 · 특징 축 ½ · 태그당 50)는 코어 profiling.selection 에 있다 — 실험과 코어가 한 구현(10-12).
# 2차 훑기의 "인기 후보 섞기"는 취향만 흐려(결과 문서 §8.1) 코어에 넣지 않았다.
D_CAT_CAP = selection.CAT_CAP
_MODEL = re.compile(r"[A-Za-z]{1,6}-?\d{2,}[A-Za-z0-9-]*")   # WH-1000XM6 · ULT900N — 색상 · 옵션만 다른 상품을 묶는 열쇠


# ---------------------------------------------------------------------------
# 입력
# ---------------------------------------------------------------------------


DEFAULT_TAGGER = "claude-opus-5-5"


def _tag_rows(path: Path, tagger: str):
    """'태그' 시트에서 그 추출 모델의 행만 — (번호, 축, 태그, 근거). 추출 모델 열이 비면 claude-opus-5-5(10-12 수작업분)로 본다."""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    for r in wb["태그"].iter_rows(min_row=2, values_only=True):
        if r and r[0] and ((r[4] if len(r) > 4 else None) or DEFAULT_TAGGER) == tagger:
            yield r[0], r[1], str(r[2]).strip(), r[3] or ""


def load_tags(path: Path, tagger: str = DEFAULT_TAGGER) -> dict[str, list[tuple[str, str]]]:
    tags: dict[str, list[tuple[str, str]]] = {}
    for no, axis, tag, _why in _tag_rows(path, tagger):
        tags.setdefault(no, []).append((axis, tag))
    return tags


def load_tag_notes(path: Path, tagger: str = DEFAULT_TAGGER) -> dict[tuple[str, str], str]:
    """(번호, 태그) → 근거 — 결과 엑셀 '입력' 시트에 태그를 어디서 뽑았는지 보이려고."""
    return {(no, tag): why for no, _axis, tag, why in _tag_rows(path, tagger)}


def load_vectors(products) -> tuple[np.ndarray, dict[str, Any]]:
    """BE 번호 순서(products)에 맞춘 상품 벡터(정규화) — source_product_id 로 product-search 행과 잇는다."""
    from profiling.settings import get_settings

    data = SEARCH / "data"
    manifest = json.loads((data / "manifest.json").read_text())
    raw = np.fromfile(data / "vectors.f32", dtype=np.float32).reshape(-1, manifest["dimensions"])
    if hashlib.sha256((data / "vectors.f32").read_bytes()).hexdigest() != manifest["vectors_sha256"]:
        raise SystemExit("vectors.f32 해시가 manifest 와 다름 — embedding/build.mjs 를 다시 돌리세요")
    model_sha = hashlib.sha256((SEARCH / "embedding/models/model_q4f16.onnx_data").read_bytes()).hexdigest()
    if model_sha != manifest["model_sha256"]:
        raise SystemExit("질의 모델과 상품 벡터의 모델이 다름(query_encoder_catalog_mismatch)")
    row_of = {sid: i for i, sid in enumerate(manifest["product_ids"])}
    eng = sa.create_engine(get_settings().DATABASE_URL, future=True)
    with eng.connect() as c:
        src = dict(c.execute(sa.text("select backend_product_id, source_product_id from ai_catalog.products "
                                     "where backend_product_id is not null")).all())
    eng.dispose()
    missing = [p.productId for p in products if src.get(p.productId) not in row_of]
    if missing:
        raise SystemExit(f"벡터가 없는 상품 {len(missing)}개(예: {missing[:5]}) — 카탈로그 스냅샷이 다름")
    v = raw[[row_of[src[p.productId]] for p in products]]
    v = v / np.linalg.norm(v, axis=1, keepdims=True)
    info = {"model": manifest["model"], "precision": manifest["precision"], "dimensions": manifest["dimensions"],
            "vectors_sha256": manifest["vectors_sha256"], "catalog_sha256": manifest["catalog_sha256"], "model_sha256": model_sha}
    return v, info


class QueryEncoder:
    """product-search/embedding/service.mjs 를 한 번 띄워 질의를 줄 단위로 보낸다(product-search Encoder 와 같은 규약, 'Query: ' 접두)."""

    def __enter__(self) -> Self:
        self.p = subprocess.Popen(["node", "service.mjs"], cwd=SEARCH / "embedding", stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        if not select.select([self.p.stdout], [], [], 120)[0] or not json.loads(self.p.stdout.readline()).get("ready"):
            self.__exit__()
            raise SystemExit("임베딩 실행기 준비 실패 — product-search 에서 npm ci · tools/download_model.py 먼저")
        return self

    def __call__(self, text: str) -> np.ndarray:
        self.p.stdin.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        v = np.asarray(json.loads(self.p.stdout.readline())["vector"], dtype=np.float32)
        return v / np.linalg.norm(v)

    def __exit__(self, *_: object) -> None:
        self.p.terminate()
        self.p.wait(timeout=5)


# ---------------------------------------------------------------------------
# 방식
# ---------------------------------------------------------------------------


def _pct(x: np.ndarray) -> np.ndarray:
    """순위 백분위(0~1) — 나보다 작은 값의 개수 ÷ (n − 1). pipeline._view_percentile 과 같은 정의."""
    s = np.sort(x)
    return np.searchsorted(s, x, side="left") / max(len(x) - 1, 1)


def _fill(picked: list[int], s0: list[int]) -> list[int]:
    seen = set(picked)
    return picked + [i for i in s0 if i not in seen][: POOL - len(picked)]


def dup_key(p) -> tuple[str, str] | None:
    """코어 selection.dup_key(같은 브랜드 + 같은 모델 코드) — 상품 객체를 받는 얇은 포장."""
    return selection.dup_key(p.name, p.brand)


def _mmr(cands: list[int], rel: np.ndarray, V: np.ndarray, lam: float, k: int) -> list[int]:
    """C(1차) 의 MMR — 관련도 rel(cands 와 같은 순서, 0~1)과 이미 고른 상품과의 최대 코사인을 λ 로 섞어 k 개."""
    cv = V[cands]
    chosen: list[int] = []
    red = np.zeros(len(cands))
    left = np.ones(len(cands), dtype=bool)
    while len(chosen) < k and left.any():
        j = int(np.argmax(np.where(left, lam * rel - (1 - lam) * red, -np.inf)))
        left[j] = False
        chosen.append(cands[j])
        red = np.maximum(red, cv @ cv[j])
    return chosen


class _ArrayEmbedding:
    """코어 ports.EmbeddingIndex 모양 — 이미 만든 상품 벡터(ranked 순서)와 질의 벡터(태그 순서)를 그대로 돌려준다."""

    version = "embed_compare"

    def __init__(self, ranked, V: np.ndarray, tags: list[str], qvecs: np.ndarray) -> None:
        self._row = {s.product.productId: i for i, s in enumerate(ranked)}
        self._V, self._q = V, {t: qvecs[k] for k, t in enumerate(tags)}

    def product_vectors(self, ids: list[int]) -> np.ndarray:
        return self._V[[self._row[i] for i in ids]]

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._q[t] for t in texts])


def strategy_d(ranked, qvecs: np.ndarray, axes: list[str], V: np.ndarray, *, penalize: bool = True,
               texts: list[str] | None = None) -> tuple[list[int], dict[str, Any]]:
    """D — **코어 selection.select_pool 그대로**(10-12 코어 반영 뒤, 실험과 코어가 한 구현). 돌려주는 목록은 ranked 의 위치.
    점수 내역(유사도 항 · 총점)은 엑셀 'D_점수내역' 표시용으로만 여기서 다시 계산한다(selection 과 같은 식)."""
    n = len(ranked)
    texts = texts or [f"tag{k}" for k in range(len(qvecs))]
    tags = [selection.Tag(a, t) for a, t in zip(axes, texts)]
    emb = _ArrayEmbedding(ranked, V, texts, qvecs) if len(qvecs) else None
    sel = selection.select_pool(ranked, POOL, allow_disliked=penalize, tags=tags if len(qvecs) else (), embedding=emb)
    pos = {s.product.productId: i for i, s in enumerate(ranked)}
    info: dict[str, Any] = {"d_cands": sel.candidates, "d_filled_disliked": sel.filled_disliked, "d_dups_skipped": sel.dups_skipped,
                            "d_sim_term": np.zeros(n), "d_total": np.zeros(n)}
    ok = np.array([i for i, s in enumerate(ranked) if not s.disliked])
    if len(qvecs) and len(ok):
        sims = V[ok] @ qvecs.T
        w = np.array([selection.AXIS_WEIGHT.get(a, 1.0) for a in axes])
        pct = np.stack([_pct(sims[:, j]) for j in range(sims.shape[1])], axis=1)
        sim_term = selection.SIM_WEIGHT * (pct * w).max(axis=1)
        info["d_sim_term"][ok] = sim_term
        info["d_total"][ok] = np.array([ranked[i].popularity + ranked[i].bias for i in ok]) + sim_term
    return [pos[p] for p in sel.product_ids], info


def strategies(ranked, qvecs: np.ndarray, V: np.ndarray, penalize: bool, axes: list[str] | None = None,
               texts: list[str] | None = None) -> dict[str, Any]:
    """ranked = rank_products 결과(후보 전체, S0 순서). V = ranked 와 같은 순서의 상품 벡터. 돌려주는 목록은 ranked 의 위치."""
    n = len(ranked)
    s0 = list(range(n))
    out: dict[str, Any] = {"S0": s0[:POOL], "sim": None, "cands": 0}
    out["D"], dinfo = strategy_d(ranked, qvecs, axes or ["카테고리"] * len(qvecs), V, penalize=penalize, texts=texts)
    out |= dinfo
    if len(qvecs) == 0:
        out |= {"A": s0[:POOL], "B": s0[:POOL], "C": s0[:POOL], "fallback": True}
        return out
    sims = V @ qvecs.T                                       # (n, 태그 수)
    rrf = np.zeros(n)
    cand: set[int] = set()
    for j in range(sims.shape[1]):
        top = np.lexsort((np.arange(n), -sims[:, j]))[:PER_TAG]   # 동점은 S0 순서
        rrf[top] += 1.0 / (RRF_K + np.arange(1, len(top) + 1))
        cand.update(top.tolist())
    cands = sorted(cand)
    disliked = np.array([s.disliked for s in ranked])
    # A — RRF 순서. 감점 모드면 비선호를 뒤로(임베딩엔 '싫음'을 넣을 수 없다)
    a = sorted(cands, key=lambda i: (penalize and bool(disliked[i]), -rrf[i], i))
    # B — 후보를 코드 점수로
    max_sim = sims.max(axis=1)
    sim_term = SIM_WEIGHT * _pct(max_sim)
    total = np.array([s.total for s in ranked]) + sim_term
    b = sorted(cands, key=lambda i: (-total[i], i))
    # C — MMR(후보 안에서)
    rel = total[cands]
    rel = (rel - rel.min()) / (rel.max() - rel.min() or 1.0)         # 1차 그대로(min-max) — D 는 백분위
    c = _mmr(cands, rel, V, MMR_LAMBDA, POOL)
    out |= {"A": _fill(a[:POOL], s0), "B": _fill(b[:POOL], s0), "C": _fill(c, s0), "fallback": False,
            "sim": max_sim, "sim_term": sim_term, "total_b": total, "rrf": rrf, "cands": len(cands)}
    return out


def check_d(r: dict[str, Any]) -> list[tuple[str, bool, str]]:
    """D 의 기대 — 30개 · 중복 없음 · 비선호 배제(비선호 아닌 상품이 30개 이상이면 0) · 이름 중복 0 · 결정성 · U09 뷰티 0 · U08 30개."""
    d, rk, m = r["st"]["D"], r["ranked"], r["m"]["D"]
    n_ok = sum(not s.disliked for s in rk)
    out = [("30개 · 중복 없음", len(d) == min(POOL, len(rk)) and len(set(d)) == len(d), f"{len(d)}개"),
           ("비선호 배제", m["비선호"] == max(0, min(POOL, len(rk)) - n_ok), f"비선호 {m['비선호']} · 비선호 아닌 후보 {n_ok}"),
           ("이름 중복 0", m["이름중복"] == 0, f"{m['이름중복']}"),
           (f"한 소분류 최대 {D_CAT_CAP}", m["최다_개수"] <= D_CAT_CAP or n_ok < POOL, m["최다_소분류"]),
           ("결정성", r["deterministic"], "")]
    if r["user"].no == "U09":
        out.append(("U09 뷰티(대분류 1) 0", not any(rk[i].product.parentCategoryId == 1 for i in d), ""))
    if r["user"].no == "U08":
        out.append(("U08 30개 채움", len(d) == POOL, f"{len(d)}"))
    return out


def metrics(pos: list[int], ranked, s0: list[int], view_pct, V: np.ndarray) -> dict[str, Any]:
    ps = [ranked[i] for i in pos]
    pv = V[pos]
    sim = np.tril(pv @ pv.T, k=-1)
    cat = Counter(s.product.categoryName for s in ps)
    top_name, top_n = cat.most_common(1)[0] if cat else ("-", 0)
    return {"소분류_종수": len(cat), "최다_소분류": f"{top_name} {top_n}", "최다_개수": top_n,
            "비선호": sum(s.disliked for s in ps), "편향_소분류": sum(s.bias > 0 for s in ps), "낮은별점_소분류": sum(s.bias < 0 for s in ps),
            "근접중복": int((sim >= NEAR_DUP).any(axis=1).sum()), "이름중복": len(pos) - len({dup_key(s.product) or i for i, s in zip(pos, ps)}), "S0_겹침": len(set(pos) & set(s0)), "조회수_백분위_평균": round(float(np.mean([view_pct(s.product.viewCount) for s in ps])), 3)}


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


def run(scenarios: Path = DEFAULT_SCENARIOS, out_dir: Path = DEFAULT_OUT, modes: tuple[str, ...] = MODES, *,
        top: int = 5, quiet: bool = False, tagger: str = DEFAULT_TAGGER) -> dict[str, Any]:
    from profiling import pipeline
    from profiling.catalog import DbCatalogReader
    from profiling.settings import get_settings
    from profiling.types import DislikedCategory, ProfileRequest, Review
    from tools.scenario_callback30.run import load_scenarios

    users, tags, notes = load_scenarios(scenarios), load_tags(scenarios, tagger), load_tag_notes(scenarios, tagger)
    if not tags:
        raise SystemExit(f"'태그' 시트에 추출 모델 {tagger!r} 행이 없음 — extract_tags.py 먼저")
    engine = sa.create_engine(get_settings().DATABASE_URL, future=True)
    catalog = DbCatalogReader(engine)
    _, products = catalog.active()
    V_all, vinfo = load_vectors(products)
    row = {p.productId: i for i, p in enumerate(products)}
    by_id = {p.productId: p for p in products}
    t_all = time.perf_counter()
    results: list[dict[str, Any]] = []
    with QueryEncoder() as enc:
        qcache: dict[str, np.ndarray] = {}
        for u in users:
            utags = tags.get(u.no, [])
            t0 = time.perf_counter()
            for _, t in utags:
                if t not in qcache:
                    qcache[t] = enc(t)
            t_enc = time.perf_counter() - t0
            qv = np.array([qcache[t] for _, t in utags]).reshape(-1, V_all.shape[1])
            again = np.array([enc(t) for _, t in utags]).reshape(qv.shape)        # 같은 질의 → 같은 벡터인가(결정성)
            rq = ProfileRequest(recipient_user_id=1, source_version=1, gift_preference=u.preference,
                                disliked_categories=[DislikedCategory(category_id=c, category_name=n) for c, n in u.disliked],
                                reviews=[Review(product_id=r["productId"], rating=r["rating"], review_text=r["reviewText"]) for r in u.reviews])
            scores = pipeline.category_scores(rq, products, catalog)
            for mode in modes:
                t1 = time.perf_counter()
                ranked = pipeline.rank_products(rq, products, dislike_mode=pipeline.DislikeMode(mode), scores=scores)
                V = V_all[[row[s.product.productId] for s in ranked]]
                axes = [a for a, _ in utags]
                st = strategies(ranked, qv, V, mode == "penalty", axes, [t for _, t in utags])
                t_rank = time.perf_counter() - t1
                st2 = strategies(ranked, again, V, mode == "penalty", axes, [t for _, t in utags])
                view_pct = pipeline._view_percentile([s.product for s in ranked])
                r = {"user": u, "mode": mode, "tags": utags, "notes": notes, "scores": scores, "by_id": by_id, "ranked": ranked, "st": st, "t_enc": t_enc, "t_rank": t_rank,
                     "deterministic": all(st[k] == st2[k] for k in STRATEGIES),
                     "m": {k: metrics(st[k], ranked, st["S0"], view_pct, V) for k in STRATEGIES}}
                r["checks"] = check_d(r)
                results.append(r)
    engine.dispose()
    if not quiet:
        print(f"\n=== 임베딩 비교 — 태그 추출 모델 {tagger} · 임베딩 {vinfo['model']} · 사용자 {len(users)}명 × 모드 {len(modes)} · 태그당 {PER_TAG} · 유사도 가중 {SIM_WEIGHT} · MMR λ {MMR_LAMBDA} ===")
        for r in results:
            if r["mode"] == results[0]["mode"]:
                print_input(r)
            print_run(r, top)
    xlsx, js = write_files(results, out_dir, vinfo, tagger)
    if not quiet:
        allc = [c for r in results for c in r["checks"]]
        print(f"\nD 검사 {sum(c[1] for c in allc)}/{len(allc)} · 결정성 {sum(r['deterministic'] for r in results)}/{len(results)} · 질의 임베딩 p95 "
              f"{np.percentile([r['t_enc'] / max(len(r['tags']), 1) for r in results if r['tags']], 95) * 1000:.1f}ms/태그(사람 평균의 p95) · 전체 {time.perf_counter() - t_all:.1f}s")
        print(f"결과 파일: {xlsx}\n          {js}")
    return {"tagger": tagger, "results": results, "xlsx": xlsx, "json": js, "vectors": vinfo, "ok": all(c[1] for r in results for c in r["checks"])}


def input_rows(r: dict[str, Any]) -> tuple[list[tuple], dict[str, str]]:
    """사용자 입력을 사람이 읽는 꼴로 — 리뷰 행(상품 이름 · 소분류 · 비고)과 요약 칸."""
    u, by_id = r["user"], r["by_id"]
    seen: set[int] = set()
    reviews = []
    for pos, rv in enumerate(u.reviews):
        p = by_id.get(rv["productId"])
        note = "카탈로그에 없음 — 건너뜀" if p is None else ("같은 상품 중복 — 앞(최신)만 씀" if rv["productId"] in seen else "")
        seen.add(rv["productId"])
        reviews.append((u.no, pos, rv["productId"], p.name[:40] if p else "-",
                        f"{p.parentCategoryName} > {p.categoryName}" if p else "-", rv["rating"], rv["reviewText"] or "", note))
    cat_name = {p.categoryId: p.categoryName for p in by_id.values()}
    summary = {
        "비선호": " · ".join(f"{n}({c})" for c, n in u.disliked) or "없음",
        "취향 문장": u.preference or "없음",
        "태그": " · ".join(f"{a}:{t}" for a, t in r["tags"]) or "없음 → 조회수 순위로 대체",
        "소분류 편향": " · ".join(f"{cat_name.get(sc.category_id, sc.category_id)} n{sc.n} 합{sc.score_sum:+d} → {sc.bias:+.2f}"
                              for sc in r["scores"]) or "없음",
    }
    return reviews, summary


def print_input(r: dict[str, Any]) -> None:
    u = r["user"]
    reviews, sm = input_rows(r)
    print(f"\n━━ {u.no} {u.alias} — {u.persona}")
    for k in ("비선호", "취향 문장", "태그", "소분류 편향"):
        print(f"   {k:<6} {sm[k]}")
    for _, pos, pid, name, cat, rating, text, note in reviews:
        print(f"   리뷰{pos} {rating}점 · {pid} {name} [{cat}]{' · ' + text if text else ''}{' ⚠ ' + note if note else ''}")


def print_run(r: dict[str, Any], top: int) -> None:
    u, st = r["user"], r["st"]
    tags = ", ".join(f"{a}:{t}" for a, t in r["tags"]) or "없음 → 조회수 순위로 대체"
    print(f"\n── {u.no} {u.alias} · {r['mode']} · 태그 {tags} · 후보 {st['cands']} · D 후보 {st['d_cands']}")
    for k in STRATEGIES:
        m = r["m"][k]
        names = " · ".join(r["ranked"][i].product.name[:14] for i in st[k][:top])
        print(f"   {LABEL[k]:<12} 소분류 {m['소분류_종수']:>2}종 · 최다 {m['최다_소분류']:<10} · 편향 {m['편향_소분류']:>2} · "
              f"비선호 {m['비선호']} · 중복 {m['근접중복']:>2}/{m['이름중복']:>2} · S0겹침 {m['S0_겹침']:>2} · 조회 {m['조회수_백분위_평균']:.2f} | {names}")
    for name, ok, detail in r["checks"]:
        if not ok:
            print(f"   ❌ D 검사 실패: {name} — {detail}")


def write_files(results, out_dir: Path, vinfo: dict[str, Any], tagger: str = DEFAULT_TAGGER) -> tuple[Path, Path]:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w.-]", "_", tagger)
    xlsx, js = out_dir / f"임베딩_비교_{safe}_{stamp}.xlsx", out_dir / f"임베딩_비교_{safe}_{stamp}.json"
    font, hfont, hfill = Font(name="Arial", size=10), Font(name="Arial", size=10, bold=True, color="FFFFFF"), PatternFill("solid", fgColor="007D77")
    wb = Workbook()

    def sheet(title, head, rows, widths, first=False):
        ws = wb.active if first else wb.create_sheet(title)
        ws.title = title
        ws.append(head)
        for c in ws[1]:
            c.font, c.fill = hfont, hfill
        for rw in rows:
            ws.append(rw)
            for c in ws[ws.max_row]:
                c.font = font
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"

    keys = ("소분류_종수", "최다_소분류", "편향_소분류", "낮은별점_소분류", "비선호", "근접중복", "이름중복", "S0_겹침", "조회수_백분위_평균")
    first = [r for r in results if r["mode"] == results[0]["mode"]]
    sheet("입력_사용자", ("번호", "별칭", "페르소나", "비선호(대분류)", "취향 문장", "리뷰 수", "태그(축:태그)", "리뷰로 계산한 소분류 편향", "보고 싶은 것"),
          [(r["user"].no, r["user"].alias, r["user"].persona, *(input_rows(r)[1][k] for k in ("비선호", "취향 문장")), len(r["user"].reviews),
            input_rows(r)[1]["태그"], input_rows(r)[1]["소분류 편향"], r["user"].note) for r in first],
          (6, 14, 30, 22, 30, 7, 50, 50, 40), first=True)
    sheet("입력_리뷰", ("번호", "순서(0=최신)", "상품 ID", "상품 이름", "대분류 > 소분류", "별점", "리뷰 글", "비고"),
          [row for r in first for row in input_rows(r)[0]], (6, 10, 8, 40, 24, 6, 30, 26))
    sheet("요약", ("번호", "별칭", "모드", "방식", "태그 수", "후보 수", *keys, "결정성"),
          [(r["user"].no, r["user"].alias, r["mode"], LABEL[k], len(r["tags"]), r["st"]["cands"], *(r["m"][k][x] for x in keys),
            "같음" if r["deterministic"] else "다름") for r in results for k in STRATEGIES],
          (6, 14, 9, 15, 8, 8, 10, 16, 10, 12, 8, 9, 9, 9, 14, 8))
    rows = []
    for r in results:
        st, rk = r["st"], r["ranked"]
        for i in range(POOL):
            line = [r["user"].no, r["mode"], i + 1]
            for k in STRATEGIES:
                s = rk[st[k][i]] if i < len(st[k]) else None
                line += [f"{s.product.name[:30]} [{s.product.categoryName}]{' ⛔' if s.disliked else ''}" if s else ""]
            rows.append(line)
    sheet("비교30", ("번호", "모드", "순위", *(LABEL[k] for k in STRATEGIES)), rows, (6, 9, 6, 48, 48, 48, 48))
    rows = []
    for r in results:
        st, rk = r["st"], r["ranked"]
        if st["fallback"]:
            continue
        for i in st["C"]:
            s = rk[i]
            rows.append((r["user"].no, r["mode"], s.product.productId, s.product.name[:40], s.product.categoryName,
                         round(float(st["sim"][i]), 4), round(float(st["sim_term"][i]), 4), round(s.popularity, 4), round(s.bias, 4),
                         s.penalty, round(float(st["total_b"][i]), 4), round(float(st["rrf"][i]), 4)))
    sheet("C_점수내역", ("번호", "모드", "상품", "이름", "소분류", "최대 코사인", "유사도 항(×2 백분위)", "인기", "편향", "감점", "B 총점", "RRF"),
          rows, (6, 9, 8, 40, 14, 11, 16, 8, 8, 7, 9, 8))
    rows = []
    for r in results:
        st, rk = r["st"], r["ranked"]
        for i in st["D"]:
            s = rk[i]
            rows.append((r["user"].no, r["mode"], s.product.productId, s.product.name[:40], s.product.categoryName, "⛔" if s.disliked else "",
                         round(float(st["d_sim_term"][i]), 4), round(s.popularity, 4), round(s.bias, 4), round(float(st["d_total"][i]), 4),
                         " ".join(dup_key(s.product) or ("", ""))))
    sheet("D_점수내역", ("번호", "모드", "상품", "이름", "소분류", "비선호(채움)", "유사도 항(축 가중)", "인기", "편향", "D 점수", "중복 열쇠"),
          rows, (6, 9, 8, 40, 14, 10, 15, 8, 8, 9, 22))
    sheet("D_검사", ("번호", "모드", "검사", "결과", "내용"),
          [(r["user"].no, r["mode"], n, "통과" if ok else "실패", d) for r in results for n, ok, d in r["checks"]], (6, 9, 22, 8, 40))
    seen = set()
    sheet("태그", ("번호", "축", "태그", "근거"), [(r["user"].no, a, t, r["notes"].get((r["user"].no, t), "")) for r in results
                                                if not (r["user"].no in seen or seen.add(r["user"].no)) for a, t in r["tags"]], (6, 10, 24, 48))
    sheet("설정", ("항목", "값"), [("태그 추출 모델", tagger), ("임베딩 모델", f"{vinfo['model']} ({vinfo['precision']}, {vinfo['dimensions']}차원)"),
                                   ("태그당 후보", PER_TAG), ("RRF 상수", RRF_K), ("유사도 가중", SIM_WEIGHT), ("MMR λ", MMR_LAMBDA),
                                   ("풀 크기", POOL), ("근접 중복 기준(코사인)", NEAR_DUP),
                                   ("D 구현", "profiling.selection.select_pool(코어)"), ("D 축 가중", json.dumps(selection.AXIS_WEIGHT, ensure_ascii=False)),
                                   ("D MMR λ", selection.MMR_LAMBDA),
                                   ("D 이름 중복", "같은 브랜드 + 같은 모델 코드"), ("D 소분류 상한", D_CAT_CAP), *vinfo.items(), ("모델 라이선스", "CC BY-NC 4.0 — 비상업. 서비스 반영 전 확인 필요")], (16, 70))
    wb.save(xlsx)
    js.write_text(json.dumps({
        "settings": {"tagger": tagger, "embedding_model": vinfo["model"], "near_dup": NEAR_DUP, "per_tag": PER_TAG, "rrf_k": RRF_K, "sim_weight": SIM_WEIGHT, "mmr_lambda": MMR_LAMBDA,
                     "d": {"impl": "profiling.selection", "axis_weight": selection.AXIS_WEIGHT, "lambda": selection.MMR_LAMBDA, "cat_cap": selection.CAT_CAP}, "vectors": vinfo},
        "runs": [{"user": r["user"].no, "mode": r["mode"], "tags": r["tags"],
                  "input": {"alias": r["user"].alias, "persona": r["user"].persona, "disliked": r["user"].disliked,
                            "preference": r["user"].preference, "reviews": r["user"].reviews,
                            "category_scores": [{"category_id": sc.category_id, "n": sc.n, "score_sum": sc.score_sum, "bias": sc.bias}
                                                for sc in r["scores"]]}, "deterministic": r["deterministic"],
                  "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in r["checks"]], "t_enc_ms": round(r["t_enc"] * 1000, 2), "t_rank_ms": round(r["t_rank"] * 1000, 2), "metrics": r["m"],
                  "pools": {k: [r["ranked"][i].product.productId for i in r["st"][k]] for k in STRATEGIES}} for r in results],
    }, ensure_ascii=False, indent=1))
    return xlsx, js


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--modes", default=",".join(MODES), help="penalty,exclude 중 쉼표로")
    ap.add_argument("--top", type=int, default=5, help="화면에 보일 상위 상품 수")
    ap.add_argument("--tagger", default=DEFAULT_TAGGER, help="'태그' 시트의 추출 모델(claude-opus-5-5 · gemma4:12b · qwen3.5:9b …)")
    a = ap.parse_args()
    return 0 if run(a.scenarios, a.out, tuple(m.strip() for m in a.modes.split(",") if m.strip()), top=a.top, tagger=a.tagger)["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
