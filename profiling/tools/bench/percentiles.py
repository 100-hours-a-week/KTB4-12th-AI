"""시간 백분위(p75 · p90 · p95 · p99) 원자료 — 시험 결과 보고서 그래프용. 서버 · LLM 없음(LLM 추출 시간은 기존 기록을 모음).

    cd profiling && uv run python -m tools.bench.percentiles [--out docs/assets/report/percentiles.json]

측정(모두 로컬 PostgreSQL · DB 카탈로그 4,231개)
  store       저장 한 번(리뷰 10개) 1,000회씩 — CTE · 잠금 커넥션(운영 경로) / CTE · 풀 커넥션 / 트랜잭션 세 문장(가이드 원안)
  app_only    앱만(TestClient) 7.6 을 순서대로 200건 — 202 · 7.7 콜백까지 · 서버 처리. 모드마다 새 프로세스:
              before = 카탈로그 로드 뒤 gc.unfreeze()(10-12 반영 전 재현) / after = 지금 코드(DbCatalogReader 가 gc.freeze) / off = GC 끔(대조)
  callback30  30개 콜백 검증 10회 반복(200건) — 하네스가 결과를 쌓아 힙이 큰 조건
  embed_query 질의 임베딩 1회(태그 28종 × 20)
  d_rank      임베딩 비교 D 포함 순위 계산(사람 10 × 5)
  llm         로컬 LLM 태그 추출 사람당 시간 — tools/embed_compare/out/태그추출_<모델>_*.json 전부(실행 횟수만큼)
p50 은 내지 않는다 — 꼬리(p95 · p99)를 본다. 표본이 작을 때 p95 는 최댓값에 가깝다(app_only · callback30 은 200건).
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import sqlalchemy as sa

PATH_76 = "/api/internal/v1/ai/profile/extract-and-pool"
BASE = 9_400_000


def pct(x: list[float]) -> dict[str, float]:
    return {f"p{p}": round(float(np.percentile(x, p)), 3) for p in (75, 90, 95, 99)} | {"max": round(max(x), 3), "n": len(x)}


def bench_store(n: int = 1000) -> dict[str, list[float]]:
    from profiling.settings import get_settings
    from profiling.stores import DbProfileRunStore, DbRecipientProfileStore
    from profiling.types import RecipientProfile, ReviewRecord

    eng = sa.create_engine(get_settings().DATABASE_URL, future=True)
    txn = [sa.text("""insert into ai_profile.recipient_profiles (recipient_user_id, source_version, preferred_tags, disliked_tags, disliked_categories,
               gift_preference_hash, category_scores) values (:rid, :sv, '[]', '[]', '[]', :ph, '[]'::jsonb)
               on conflict (recipient_user_id) do update set source_version=excluded.source_version, gift_preference_hash=excluded.gift_preference_hash,
               category_scores=excluded.category_scores, updated_at=now()
               where ai_profile.recipient_profiles.source_version <= excluded.source_version returning recipient_user_id"""),
           sa.text("delete from ai_profile.recipient_reviews where recipient_user_id=:rid and product_id <> all(cast(:pids as bigint[]))"),
           sa.text("""insert into ai_profile.recipient_reviews (recipient_user_id, product_id, rating, review_hash, position)
               select :rid, * from unnest(cast(:pids as bigint[]), cast(:ratings as smallint[]), cast(:hashes as text[]), cast(:positions as smallint[]))
               on conflict (recipient_user_id, product_id) do update set rating=excluded.rating, review_hash=excluded.review_hash,
               position=excluded.position, updated_at=now()""")]

    def prof(rid: int, sv: int) -> RecipientProfile:
        recs = [ReviewRecord(product_id=10 + k, rating=1 + (k + sv) % 5, position=k, review_hash=f"{k:064d}") for k in range(10)]
        return RecipientProfile(recipient_user_id=rid, source_version=sv, reviews=recs, gift_preference_hash="a" * 64)

    def three(p: RecipientProfile) -> None:
        rv = p.reviews
        prm = {"rid": p.recipient_user_id, "sv": p.source_version, "ph": p.gift_preference_hash, "pids": [r.product_id for r in rv],
               "ratings": [r.rating for r in rv], "hashes": [r.review_hash for r in rv], "positions": [r.position for r in rv]}
        with eng.begin() as c:
            if c.execute(txn[0], prm).first() is not None:
                c.execute(txn[1], prm)
                c.execute(txn[2], prm)

    clean(eng)
    store, runs = DbRecipientProfileStore(eng), DbProfileRunStore(eng)
    for _ in range(50):
        store.upsert(prof(BASE + 900, 1))
    out: dict[str, list[float]] = {"CTE · 잠금 커넥션": [], "CTE · 풀 커넥션": [], "세 문장 · 풀 커넥션": []}
    with runs.run_lock(BASE + 902, 1):
        for i in range(n):
            p = prof(BASE + 902, i + 1); t = time.perf_counter(); store.upsert(p); out["CTE · 잠금 커넥션"].append((time.perf_counter() - t) * 1000)
    for i in range(n):
        p = prof(BASE + 901, i + 1); t = time.perf_counter(); store.upsert(p); out["CTE · 풀 커넥션"].append((time.perf_counter() - t) * 1000)
    for i in range(n):
        p = prof(BASE + 903, i + 1); t = time.perf_counter(); three(p); out["세 문장 · 풀 커넥션"].append((time.perf_counter() - t) * 1000)
    clean(eng)
    eng.dispose()
    return out


def clean(eng: sa.Engine, n: int = 1000) -> None:
    with eng.begin() as c:
        for t in ("recipient_reviews", "recipient_profiles", "profile_runs"):
            c.execute(sa.text(f"delete from ai_profile.{t} where recipient_user_id between {BASE} and {BASE + n + 10}"))


def app_only(mode: str, n: int = 200) -> dict:
    """새 프로세스에서 부른다(GC 상태 · 앱 상태가 섞이지 않게)."""
    import httpx

    os.environ.update({"PROFILING_CATALOG_SOURCE": "db", "PROFILING_SERVICE_TOKEN": "t", "PROFILING_DISLIKE_MODE": "penalty", "PROFILING_LOG_LEVEL": "ERROR"})
    from fastapi.testclient import TestClient

    from profiling.backend import HttpBackendPort
    from profiling.settings import get_settings
    from tools.scenario_callback30.run import DEFAULT_SCENARIOS, load_scenarios

    get_settings.cache_clear()
    from profiling.main import app

    users = load_scenarios(DEFAULT_SCENARIOS)
    eng = sa.create_engine(get_settings().DATABASE_URL)
    clean(eng, n)
    got: dict[int, float] = {}

    def h(req: httpx.Request) -> httpx.Response:
        b = json.loads(req.content)
        got[b["recipientUserId"]] = time.perf_counter()
        return httpx.Response(200, json={"message": "ok", "data": {"recipientUserId": b["recipientUserId"], "sourceVersion": b["sourceVersion"],
                                                                  "profileStatus": "COMPLETED"}})

    cb, t202 = [], []
    with TestClient(app) as cl:
        app.state.backend = HttpBackendPort("http://be", "t", 5.0, transport=httpx.MockTransport(h))
        app.state.catalog.active()
        if mode == "before":                        # 지금 코드는 카탈로그 로드 뒤 gc.freeze() 한다 — 반영 전을 재현하려고 푼다
            gc.unfreeze()
        elif mode == "off":
            gc.disable()
        g0 = gc.get_stats()[2]["collections"]
        for i in range(n):
            rid = BASE + i + 1
            t0 = time.perf_counter()
            cl.post(PATH_76, json=users[i % len(users)].body(rid), headers={"Authorization": "Bearer t"})
            t202.append((time.perf_counter() - t0) * 1000)
            while rid not in got and time.perf_counter() - t0 < 10:
                time.sleep(0.0005)
            cb.append((got[rid] - t0) * 1000)
        gen2 = gc.get_stats()[2]["collections"] - g0
    with eng.connect() as c:
        srv = [float(r[0]) for r in c.execute(sa.text(f"select extract(epoch from updated_at - created_at) * 1000 from ai_profile.profile_runs "
                                                      f"where recipient_user_id between {BASE} and {BASE + n}"))]
    clean(eng, n)
    return {"mode": mode, "gen2": gen2, "accept_202": t202, "callback": cb, "server": srv}


def callback30(reps: int = 10) -> list[dict]:
    from profiling.settings import get_settings
    from tools.scenario_callback30 import run as sc

    saved, rows = dict(os.environ), []
    for k in range(reps):
        with tempfile.TemporaryDirectory() as d:
            try:
                res = sc.run(out_dir=Path(d), quiet=True)
            finally:
                os.environ.clear()
                os.environ.update(saved)
                get_settings.cache_clear()
            for order, r in enumerate(json.loads(res["json"].read_text())):
                rows.append({"rep": k, "order": order, "user": r["user"], "mode": r["mode"], **r["timing_ms"]})
    return rows


def embed() -> tuple[list[float], list[float]]:
    import tools.embed_compare.run as ec

    tags = sorted({t for v in ec.load_tags(ec.DEFAULT_SCENARIOS).values() for _, t in v})
    q = []
    with ec.QueryEncoder() as enc:
        for t in tags:
            enc(t)
        for _ in range(20):
            for t in tags:
                s = time.perf_counter(); enc(t); q.append((time.perf_counter() - s) * 1000)
    rk = []
    for _ in range(5):
        with tempfile.TemporaryDirectory() as d:
            rk += [r["t_rank"] * 1000 for r in ec.run(modes=("penalty",), quiet=True, out_dir=Path(d))["results"]]
    return q, rk


def llm_logs() -> dict[str, list[float]]:
    from tools.embed_compare.run import DEFAULT_OUT

    out: dict[str, list[float]] = {}
    for f in sorted(DEFAULT_OUT.glob("태그추출_*_2*.json")):
        if "비교" in f.name:
            continue
        d = json.loads(f.read_text())
        secs = [u["seconds"] for u in d["users"].values() if u["seconds"] is not None]   # 대화 안 원출력(--from-json)은 시간이 없다
        if secs:
            out.setdefault(d["model"], []).extend(secs)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path("docs/assets/report/percentiles.json"))
    ap.add_argument("--app-only", choices=("before", "after", "off"), help=argparse.SUPPRESS)   # 내부용 — 새 프로세스
    a = ap.parse_args()
    if a.app_only:
        print(json.dumps(app_only(a.app_only)))
        return 0
    raw: dict = {"measured_at": time.strftime("%Y-%m-%d %H:%M"), "store": bench_store()}
    print("store", {k: pct(v) for k, v in raw["store"].items()}, flush=True)
    raw["app_only"] = {}
    for m in ("before", "after", "off"):
        r = subprocess.run([sys.executable, "-m", "tools.bench.percentiles", "--app-only", m], capture_output=True, text=True, check=True)
        raw["app_only"][m] = json.loads(r.stdout.strip().splitlines()[-1])
        print("app_only", m, "gen2", raw["app_only"][m]["gen2"], {k: pct(raw["app_only"][m][k]) for k in ("callback", "server")}, flush=True)
    raw["callback30"] = callback30()
    print("callback30", {k: pct([r[k] for r in raw["callback30"]]) for k in ("callback", "server")}, flush=True)
    raw["embed_query"], raw["d_rank"] = embed()
    print("embed_query", pct(raw["embed_query"]), "d_rank", pct(raw["d_rank"]), flush=True)
    raw["llm"] = llm_logs()
    print("llm", {k: pct(v) for k, v in raw["llm"].items()})
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(raw, ensure_ascii=False))
    print(f"→ {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
