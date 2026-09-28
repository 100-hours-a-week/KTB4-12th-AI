"""부하 시험 하네스 — 7.6 을 동시에 N건 보내고, 그동안 /health 지연·DB 커넥션·완료 시각을 잰다.

전제 (profiling/ 에서, 서버는 사용자 동의 뒤에 띄운다):
  - 가짜 백엔드 :8081  `uv run uvicorn tools.fake_backend.app:app --port 8081`  — 7.7 싱크(모르는 수신자도 200), 실패 주입은 --fake-mode
  - AI :8000           `PROFILING_CATALOG_SOURCE=db PROFILING_BACKEND_BASE_URL=http://localhost:8081 PROFILING_SLOTS=1 \\
                          uv run uvicorn profiling.main:app --port 8000 --timeout-graceful-shutdown 5`   (--reload 없이)
  - AI DB 는 profiling 설정(PROFILING_DATABASE_URL)으로 직접 읽는다 — 시각은 전부 profile_runs 의 DB 시각 기준
    (가짜 백엔드의 receivedAt 은 초 단위라 쓰지 않는다)

실행 예:
  uv run python -m tools.loadtest.run --n 100 --concurrency 100 --base 900001 --cleanup --yes --expect-slots 1 \\
      --label S-A1 --out tools/loadtest/results/S-A1.json
  uv run python -m tools.loadtest.run --mode be --n 200 --base 900001 --wait 180 --ai-log tools/loadtest/results/ai-S-C.log ...
  uv run python -m tools.loadtest.run --cleanup-only --base 900001 --n 100 --yes        (RUNNING 이 남았으면 AI 재기동 뒤)

모드:
  burst  하네스가 7.6 을 N건 보낸다(수신자 base~base+n-1, 각각 sourceVersion 하나). 동시성은 --concurrency
  be     보내지 않는다. BE(develop)가 보내는 동안 AI 쪽만 잰다 — 도착 시각은 --ai-log 의 "7.6 접수" 줄에서

수신자 번호는 900000 이상만 쓴다(실제·시드 사용자 보호). --cleanup 은 --yes 와 함께, 그 구간의 실행 기록·프로필 행을 지운다.
같은 (수신자, 버전)이 이미 있으면 AI 는 재분석 없이 재전송(RESEND)하므로, 구간이 비어 있지 않으면 --cleanup 없이 시작하지 않는다.
결과: stdout 마크다운 표 + --out JSON. 종료 코드 0 완료 · 1 미완료(시간 초과) · 2 사전 점검·가드 실패.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import re
import statistics
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa

from profiling.settings import get_settings
from tools.be_integration.drive import Ai, get_json, log, wait

EXTRACT_PATH = "/api/internal/v1/ai/profile/extract-and-pool"
RECIPIENT_FLOOR = 900_000            # 이 아래는 실제·시드 사용자일 수 있다 — 정리 금지
MAX_N = 10_000
TERMINAL = ("DELIVERED", "SUPERSEDED", "FAILED", "RESULT_READY")     # RUNNING 만 미종료
ROOT_CATEGORIES = {1: "뷰티", 2: "패션", 3: "카페·디저트", 4: "식품", 5: "생활",
                   6: "디지털·가전", 7: "취미·여가", 8: "유아·키즈", 9: "반려동물", 10: "상품권"}   # BE 시드 V10 대분류 id·이름
FAKE_MODES = ("ok", "409", "400", "500", "timeout")
AI_INTAKE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) .*7\.6 접수 recipient=(\d+) source_version=(\d+)")


@dataclass
class Post:
    rid: int
    t_sent: float          # t0 기준 경과초 (보낸 시각)
    ms: float              # 응답까지 걸린 ms (오류·타임아웃 포함)
    status: int | None
    error: str | None = None


@dataclass
class Probe:
    t: float
    ms: float
    status: int | None
    error: str | None
    running: int | None
    submitted: int | None
    undelivered: int | None


@dataclass
class DbSample:
    t: float
    connections: int       # 하네스 자신을 뺀 커넥션 수
    idle_in_tx: int
    running_rows: int      # 구간의 RUNNING 행 수
    terminal_recipients: int


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------


class LoadAi(Ai):
    """drive.Ai + 수신자 구간 질의. application_name=loadtest 로 자기 커넥션을 pg_stat_activity 에서 뺀다."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.engine = sa.create_engine(get_settings().DATABASE_URL, future=True, pool_size=2, max_overflow=2,
                                       connect_args={"application_name": "loadtest"})

    def db_now(self) -> datetime:
        with self.engine.connect() as c:
            return c.execute(sa.text("select now()")).scalar_one()

    def rows(self, lo: int, hi: int) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(sa.text(
                "select recipient_user_id, source_version, status, attempt, callback_attempts, created_at, updated_at "
                "from ai_profile.profile_runs where recipient_user_id between :lo and :hi order by recipient_user_id"),
                {"lo": lo, "hi": hi}).mappings().all()]

    def progress(self, lo: int, hi: int) -> tuple[int, int]:
        """(종료 상태에 든 수신자 수, RUNNING 행 수)."""
        with self.engine.connect() as c:
            row = c.execute(sa.text(
                "select count(distinct recipient_user_id) filter (where status <> 'RUNNING'), "
                "       count(*) filter (where status = 'RUNNING') "
                "from ai_profile.profile_runs where recipient_user_id between :lo and :hi"), {"lo": lo, "hi": hi}).one()
            return int(row[0]), int(row[1])

    def activity(self) -> tuple[int, int]:
        """(하네스 제외 클라이언트 커넥션 수, 그중 idle in transaction 수)."""
        with self.engine.connect() as c:
            row = c.execute(sa.text(
                "select count(*) filter (where application_name <> 'loadtest'), "
                "       count(*) filter (where application_name <> 'loadtest' and state = 'idle in transaction') "
                "from pg_stat_activity where datname = current_database() and backend_type = 'client backend'")).one()
            return int(row[0]), int(row[1])

    def cleanup_range(self, lo: int, hi: int) -> tuple[int, int]:
        with self.engine.begin() as c:
            n_runs = c.execute(sa.text("delete from ai_profile.profile_runs where recipient_user_id between :lo and :hi"),
                               {"lo": lo, "hi": hi}).rowcount
            n_prof = c.execute(sa.text("delete from ai_profile.recipient_profiles where recipient_user_id between :lo and :hi"),
                               {"lo": lo, "hi": hi}).rowcount
        return n_runs, n_prof


# ---------------------------------------------------------------------------
# 부하·관측 코루틴
# ---------------------------------------------------------------------------


def payload(rid: int, sv: int, cats: list[int]) -> dict[str, Any]:
    return {"recipientUserId": rid, "sourceVersion": sv,
            "dislikedCategories": [{"categoryId": c, "categoryName": ROOT_CATEGORIES.get(c, f"대분류{c}")} for c in cats],
            "giftPreference": None, "reviews": []}


async def burst(client: httpx.AsyncClient, url: str, headers: dict[str, str], rids: list[int], sv: int,
                cats_of, concurrency: int, timeout: float, t0: float) -> list[Post]:
    sem = asyncio.Semaphore(concurrency)

    async def one(rid: int) -> Post:
        async with sem:
            t = time.perf_counter()
            try:
                r = await client.post(url, json=payload(rid, sv, cats_of(rid)), headers=headers, timeout=timeout)
                return Post(rid, t - t0, (time.perf_counter() - t) * 1000, r.status_code)
            except httpx.HTTPError as e:            # 타임아웃·연결 실패 — 서버는 계속 처리할 수 있으므로 완료는 DB 로 센다
                return Post(rid, t - t0, (time.perf_counter() - t) * 1000, None, type(e).__name__)

    return list(await asyncio.gather(*(one(r) for r in rids)))


async def probe_health(client: httpx.AsyncClient, ai: str, interval: float, timeout: float,
                       stop: asyncio.Event, out: list[Probe], t0: float) -> None:
    """순차 프로브 — 한 번에 하나만. 멈춘 프로브는 그 ms 자체가 표본이다."""
    while not stop.is_set():
        t = time.perf_counter()
        try:
            r = await client.get(f"{ai}/health", timeout=timeout)
            b = r.json() if r.status_code == 200 else {}
            sup, store = b.get("supervisor", {}), b.get("store", {})
            out.append(Probe(t - t0, (time.perf_counter() - t) * 1000, r.status_code, None,
                             sup.get("running"), sup.get("submitted"), store.get("undelivered")))
        except httpx.HTTPError as e:
            out.append(Probe(t - t0, (time.perf_counter() - t) * 1000, None, type(e).__name__, None, None, None))
        await asyncio.sleep(interval)


async def probe_db(ai_db: LoadAi, lo: int, hi: int, interval: float, stop: asyncio.Event,
                   out: list[DbSample], t0: float) -> None:
    while not stop.is_set():
        t = time.perf_counter()
        conns, idle_tx = await asyncio.to_thread(ai_db.activity)
        done, running = await asyncio.to_thread(ai_db.progress, lo, hi)
        out.append(DbSample(t - t0, conns, idle_tx, running, done))
        await asyncio.sleep(interval)


async def wait_terminal(ai_db: LoadAi, lo: int, hi: int, n: int, timeout: float, step: float) -> tuple[bool, float]:
    t = time.perf_counter()
    while time.perf_counter() - t < timeout:
        done, running = await asyncio.to_thread(ai_db.progress, lo, hi)
        if done >= n and running == 0:
            return True, time.perf_counter() - t
        await asyncio.sleep(step)
    return False, timeout


# ---------------------------------------------------------------------------
# 집계 (순수 — DB·네트워크 없음, 단위 시험 대상)
# ---------------------------------------------------------------------------


def pct(values: list[float], p: int) -> float | None:
    vals = sorted(values)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    return statistics.quantiles(vals, n=100, method="inclusive")[p - 1]


def dist(values: list[float]) -> dict[str, Any]:
    return {"n": len(values), "p50": pct(values, 50), "p95": pct(values, 95), "max": max(values, default=None)}


def parse_ai_log(path: Path, lo: int, hi: int) -> list[tuple[datetime, int, int]]:
    """AI 로그의 '7.6 접수' 줄 → (도착 시각, 수신자, 버전). 로그 시각은 naive 로컬 시각이라 로컬 tz 를 붙인다."""
    tz = datetime.now(UTC).astimezone().tzinfo
    out: list[tuple[datetime, int, int]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = AI_INTAKE.match(line)
        if not m:
            continue
        rid = int(m.group(2))
        if lo <= rid <= hi:
            out.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=tz), rid, int(m.group(3))))
    return out


def arrival_stats(arrivals: list[tuple[datetime, int, int]], cluster_gap_s: float = 2.0) -> dict[str, Any]:
    """도착 간격과 묶음(틱). 간격이 cluster_gap_s 를 넘으면 새 묶음."""
    ts = sorted(a[0] for a in arrivals)
    if not ts:
        return {"n": 0, "gap_ms": dist([]), "clusters": [], "max_per_second": 0}
    gaps = [(b - a).total_seconds() * 1000 for a, b in itertools.pairwise(ts)]
    clusters: list[dict[str, Any]] = []
    start, size = ts[0], 1
    for a, b in itertools.pairwise(ts):
        if (b - a).total_seconds() > cluster_gap_s:
            clusters.append({"size": size, "span_s": round((a - start).total_seconds(), 3)})
            start, size = b, 1
        else:
            size += 1
    clusters.append({"size": size, "span_s": round((ts[-1] - start).total_seconds(), 3)})
    per_second = Counter(t.replace(microsecond=0) for t in ts)
    return {"n": len(ts), "gap_ms": dist(gaps), "clusters": clusters, "max_per_second": max(per_second.values())}


def summarize(posts: list[Post], probes: list[Probe], db_samples: list[DbSample], rows: list[dict[str, Any]], *,
              n: int, threshold_ms: float, t0_db: datetime, health_before: dict | None, health_after: dict | None,
              received_before: int, received_after: int, arrivals: list[tuple[datetime, int, int]] | None = None) -> dict[str, Any]:
    ok = [p.ms for p in posts if p.status == 202]
    terminal = [r for r in rows if r["status"] != "RUNNING"]

    def elapsed(ts: datetime) -> float:                 # DB 시각 → t0 기준 경과초
        return (ts - t0_db).total_seconds()

    sent: dict[int, float] = {p.rid: p.t_sent for p in posts if p.status == 202}
    if not sent and arrivals:
        sent = {rid: elapsed(at) for at, rid, _ in arrivals}
    e2e = [(elapsed(r["updated_at"]) - sent[r["recipient_user_id"]]) * 1000 for r in terminal if r["recipient_user_id"] in sent]
    slot = [(r["updated_at"] - r["created_at"]).total_seconds() * 1000 for r in terminal]
    span = ((max(r["updated_at"] for r in terminal) - min(r["created_at"] for r in terminal)).total_seconds()
            if terminal else None)

    def posted_by(t: float) -> int:
        return sum(1 for p in posts if p.status == 202 and p.t_sent + p.ms / 1000 <= t)

    waiting = [max(0, posted_by(s.t) - s.terminal_recipients - s.running_rows) for s in db_samples] if posts else []
    sup_before = (health_before or {}).get("supervisor", {}).get("submitted")
    sup_after = (health_after or {}).get("supervisor", {}).get("submitted")
    return {
        "post": {"status_counts": dict(Counter(str(p.status or p.error) for p in posts)), "ms": dist(ok),
                 "over_10s": sum(p.ms > 10_000 for p in posts),
                 "burst_s": max((p.t_sent + p.ms / 1000 for p in posts), default=None)},
        "health": {"probes": len(probes), "failed": sum(q.status != 200 for q in probes),
                   "over_threshold": sum(q.ms > threshold_ms for q in probes), "ms": dist([q.ms for q in probes]),
                   "worst": sorted(({"t": round(q.t, 2), "ms": round(q.ms, 1), "status": q.status} for q in probes),
                                   key=lambda w: -w["ms"])[:5],
                   "undelivered_max": max((q.undelivered or 0) for q in probes) if probes else None},
        "db": {"connections_max": max((s.connections for s in db_samples), default=None),
               "idle_in_tx_max": max((s.idle_in_tx for s in db_samples), default=None),
               "waiting_max": max(waiting, default=None)},
        "runs": {"rows": len(rows), "by_status": dict(Counter(r["status"] for r in rows)),
                 "recipients_terminal": len({r["recipient_user_id"] for r in terminal}),
                 "complete": len({r["recipient_user_id"] for r in terminal}) >= n,
                 "time_to_all_terminal_s": max((elapsed(r["updated_at"]) for r in terminal), default=None),
                 "throughput_per_s": (len(terminal) / span) if span else None,
                 "slot_ms": dist(slot), "e2e_ms": dist(e2e),
                 "callback_attempts_total": sum(r["callback_attempts"] for r in rows),
                 "multi_row_recipients": sum(1 for c in Counter(r["recipient_user_id"] for r in rows).values() if c > 1)},
        "supervisor_submitted_delta": (sup_after - sup_before) if sup_before is not None and sup_after is not None else None,
        "received_delta": received_after - received_before,
        "arrivals": arrival_stats(arrivals) if arrivals else None,
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def _dist_row(d: dict[str, Any]) -> str:
    return f"n={d['n']} · p50 {_fmt(d['p50'])} · p95 {_fmt(d['p95'])} · max {_fmt(d['max'])}"


def markdown(s: dict[str, Any]) -> str:
    p, h, db, r = s["post"], s["health"], s["db"], s["runs"]
    lines = ["| 항목 | 값 |", "|---|---|",
             f"| 7.6 응답 | {p['status_counts']} · ms {_dist_row(p['ms'])} · 10초 초과 {p['over_10s']} · 버스트 {_fmt(p['burst_s'])}s |",
             f"| /health | 프로브 {h['probes']} · 실패 {h['failed']} · 임계 초과 {h['over_threshold']} · ms {_dist_row(h['ms'])} · undelivered 최대 {_fmt(h['undelivered_max'])} |",
             f"| 완료 | 행 {r['rows']} {r['by_status']} · 종료 수신자 {r['recipients_terminal']} ({'완료' if r['complete'] else '미완료'}) · 전부 종료까지 {_fmt(r['time_to_all_terminal_s'])}s · 처리량 {_fmt(r['throughput_per_s'])}/s |",
             f"| 슬롯 시간 ms | {_dist_row(r['slot_ms'])} |",
             f"| 접수→종료 ms | {_dist_row(r['e2e_ms'])} |",
             f"| DB | 커넥션 최대 {_fmt(db['connections_max'])} · idle in tx 최대 {_fmt(db['idle_in_tx_max'])} · 대기 최대 {_fmt(db['waiting_max'])} |",
             f"| 콜백 | 시도 합 {r['callback_attempts_total']} · 수신자당 2행 이상 {r['multi_row_recipients']} · /received Δ {s['received_delta']} · submitted Δ {_fmt(s['supervisor_submitted_delta'])} |"]
    if s.get("arrivals"):
        a = s["arrivals"]
        lines.append(f"| 도착(BE) | {a['n']}건 · 간격 ms {_dist_row(a['gap_ms'])} · 묶음 {a['clusters']} · 초당 최대 {a['max_per_second']} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _git_head() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False).stdout.strip() or None
    except OSError:
        return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ai", default="http://127.0.0.1:8000")
    ap.add_argument("--fake", default="http://127.0.0.1:8081", help="가짜 백엔드 콘솔. 빈 문자열이면 건드리지 않는다")
    ap.add_argument("--token", default=None, help="7.6 Bearer 토큰. 기본은 settings.SERVICE_TOKEN(비어 있으면 헤더 없음)")
    ap.add_argument("--mode", choices=("burst", "be"), default="burst")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=100)
    ap.add_argument("--base", type=int, default=900_001, help="첫 수신자 번호 (900000 이상)")
    ap.add_argument("--source-version", type=int, default=1)
    ap.add_argument("--dislike", default="rotate", help="대분류 id 목록(1,2) 또는 rotate(수신자마다 1~10 순환)")
    ap.add_argument("--fake-mode", choices=FAKE_MODES, default=None, help="시작 전 가짜 백엔드 실패 주입 모드. 끝나면 ok 로 되돌린다")
    ap.add_argument("--health-interval", type=float, default=0.25)
    ap.add_argument("--health-threshold-ms", type=float, default=1000)
    ap.add_argument("--probe-timeout", type=float, default=10)
    ap.add_argument("--db-interval", type=float, default=0.5)
    ap.add_argument("--post-timeout", type=float, default=30)
    ap.add_argument("--wait", type=float, default=120, help="전부 종료까지 기다리는 최대 초")
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--tail", type=float, default=5, help="종료 뒤 더 관찰하는 초")
    ap.add_argument("--cleanup", action="store_true", help="시작 전·끝난 뒤 구간의 실행 기록·프로필 삭제 (--yes 필요)")
    ap.add_argument("--cleanup-only", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--expect-slots", type=int, default=None, help="/health supervisor.slots 가 이 값이 아니면 중단")
    ap.add_argument("--ai-log", type=Path, default=None, help="be 모드: AI 로그 파일(7.6 접수 줄에서 도착 시각)")
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-samples", action="store_true", help="JSON 에 표본 배열을 넣지 않는다")
    return ap.parse_args(argv)


def _guard(a: argparse.Namespace) -> str | None:
    if not (1 <= a.n <= MAX_N):
        return f"--n 은 1~{MAX_N}"
    if a.concurrency < 1:
        return "--concurrency 는 1 이상"
    a.concurrency = min(a.concurrency, a.n)          # n 보다 큰 동시성은 뜻이 없다
    if (a.cleanup or a.cleanup_only) and not a.yes:
        return "--cleanup 은 --yes 와 함께"
    if (a.cleanup or a.cleanup_only) and a.base < RECIPIENT_FLOOR:
        return f"--base 는 {RECIPIENT_FLOOR} 이상이어야 정리한다 (실제·시드 사용자 보호)"
    if a.mode == "be" and a.ai_log is None:
        log("주의: be 모드인데 --ai-log 가 없다 — 접수→종료 시간을 못 잰다(슬롯 시간·완료만)")
    return None


def _cats_of(a: argparse.Namespace):
    if a.dislike == "rotate":
        return lambda rid: [((rid - a.base) % 10) + 1]
    fixed = [int(x) for x in a.dislike.split(",") if x.strip()]
    return lambda _rid: fixed


async def _run(a: argparse.Namespace, ai_db: LoadAi, lo: int, hi: int, health_before: dict, received_before: int) -> dict[str, Any]:
    token = a.token if a.token is not None else get_settings().SERVICE_TOKEN
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    stop = asyncio.Event()
    probes: list[Probe] = []
    db_samples: list[DbSample] = []
    posts: list[Post] = []
    limits = httpx.Limits(max_connections=a.concurrency + 8, max_keepalive_connections=a.concurrency)
    async with httpx.AsyncClient(limits=limits) as post_client, httpx.AsyncClient() as probe_client:
        t0 = time.perf_counter()
        t0_db = await asyncio.to_thread(ai_db.db_now)
        t0_wall = datetime.now(UTC)
        tasks = [asyncio.create_task(probe_health(probe_client, a.ai, a.health_interval, a.probe_timeout, stop, probes, t0)),
                 asyncio.create_task(probe_db(ai_db, lo, hi, a.db_interval, stop, db_samples, t0))]
        if a.mode == "burst":
            rids = list(range(lo, hi + 1))
            posts = await burst(post_client, f"{a.ai}{EXTRACT_PATH}", headers, rids, a.source_version, _cats_of(a),
                                a.concurrency, a.post_timeout, t0)
            log(f"버스트 끝: {dict(Counter(str(p.status or p.error) for p in posts))} · {time.perf_counter() - t0:.2f}s")
        done, took = await wait_terminal(ai_db, lo, hi, a.n, a.wait, a.poll)
        log(f"{'전부 종료' if done else '시간 초과'} ({took:.1f}s) · {a.tail}s 더 관찰")
        await asyncio.sleep(a.tail)
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    rows = await asyncio.to_thread(ai_db.rows, lo, hi)
    health_after = get_json(f"{a.ai}/health") or {}
    received_after = _received_count(a.fake)
    arrivals = parse_ai_log(a.ai_log, lo, hi) if (a.mode == "be" and a.ai_log and a.ai_log.exists()) else None
    summary = summarize(posts, probes, db_samples, rows, n=a.n, threshold_ms=a.health_threshold_ms, t0_db=t0_db,
                        health_before=health_before, health_after=health_after,
                        received_before=received_before, received_after=received_after, arrivals=arrivals)
    return {"summary": summary, "complete": done,
            "meta": {"label": a.label, "mode": a.mode, "n": a.n, "concurrency": a.concurrency, "base": lo, "source_version": a.source_version,
                     "fake_mode": a.fake_mode or "ok", "t0_wall": t0_wall.isoformat(timespec="milliseconds"), "t0_db": t0_db.isoformat(),
                     "git": _git_head(), "health_before": health_before, "health_after": health_after,
                     "args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(a).items()}},
            "samples": {"posts": [asdict(p) for p in posts], "probes": [asdict(q) for q in probes],
                        "db": [asdict(s) for s in db_samples], "rows": rows,
                        "arrivals": [(at.isoformat(), rid, sv) for at, rid, sv in arrivals] if arrivals else None}}


def _received_count(fake: str) -> int:
    if not fake:
        return 0
    return int((get_json(f"{fake}/received") or {}).get("count", 0))


def _fake_mode(fake: str, mode: str) -> bool:
    try:
        r = httpx.put(f"{fake}/console/mode", json={"mode": mode}, timeout=5)
        return r.status_code == 200 and r.json().get("mode") == mode
    except httpx.HTTPError:
        return False


def main(argv: list[str] | None = None) -> int:
    a = _parse_args(argv)
    why = _guard(a)
    if why:
        log(f"중단: {why}")
        return 2
    lo, hi = a.base, a.base + a.n - 1
    ai_db = LoadAi(a.ai)

    if a.cleanup_only:
        _, running = ai_db.progress(lo, hi)
        if running:
            log(f"중단: 구간 {lo}~{hi} 에 RUNNING {running}행 — AI 를 내린 뒤(잠금 해제) 다시")
            return 2
        n_runs, n_prof = ai_db.cleanup_range(lo, hi)
        log(f"정리: runs {n_runs} · profiles {n_prof} (수신자 {lo}~{hi})")
        return 0

    # 사전 점검 — AI
    health, _ = wait(lambda: get_json(f"{a.ai}/health"), 30, 1)
    if not health or not health.get("catalog", {}).get("active"):
        log(f"중단: AI /health 가 없거나 활성 카탈로그가 없다: {health}")
        return 2
    slots = health.get("supervisor", {}).get("slots")
    if a.expect_slots is not None and slots != a.expect_slots:
        log(f"중단: supervisor.slots={slots}, 기대 {a.expect_slots} — PROFILING_SLOTS 로 띄웠는지 확인")
        return 2
    log(f"AI: catalog {health['catalog'].get('products')}건 · migration {health.get('store', {}).get('migration')} · slots {slots}")

    # 사전 점검 — 구간
    existing = ai_db.rows(lo, hi)
    if existing:
        if not a.cleanup:
            log(f"중단: 구간 {lo}~{hi} 에 기존 행 {len(existing)}개 — 같은 (수신자, 버전)은 재전송 판정이 되므로 --cleanup --yes 로 지우거나 --base 를 바꾼다")
            return 2
        if any(r["status"] == "RUNNING" for r in existing):
            log("중단: 구간에 RUNNING 행이 있다 — AI 를 내린 뒤 --cleanup-only")
            return 2
        n_runs, n_prof = ai_db.cleanup_range(lo, hi)
        log(f"사전 정리: runs {n_runs} · profiles {n_prof}")

    # 사전 점검 — 가짜 백엔드
    mode_set = False
    if a.fake:
        cur = get_json(f"{a.fake}/console/mode")
        if cur is None:
            log(f"중단: 가짜 백엔드 {a.fake} 가 응답하지 않는다 (--fake '' 로 건너뛸 수 있다)")
            return 2
        if a.fake_mode and a.fake_mode != cur.get("mode"):
            mode_set = _fake_mode(a.fake, a.fake_mode)
            if not mode_set:
                log("중단: 가짜 백엔드 모드를 바꾸지 못했다")
                return 2
        try:
            httpx.delete(f"{a.fake}/received", timeout=5)
        except httpx.HTTPError:
            pass
    received_before = _received_count(a.fake)
    log(f"시작: {a.mode} · n={a.n} c={a.concurrency} · 수신자 {lo}~{hi} v{a.source_version} · fake {a.fake_mode or 'ok'} · label {a.label}")

    try:
        result = asyncio.run(_run(a, ai_db, lo, hi, health, received_before))
    finally:
        if mode_set:
            _fake_mode(a.fake, "ok")

    print(markdown(result["summary"]))
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        if a.no_samples:
            result.pop("samples", None)
        a.out.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        log(f"저장: {a.out}")

    if a.cleanup:
        _, running = ai_db.progress(lo, hi)
        if running:
            log(f"정리 보류: RUNNING {running}행 남음 — AI 를 내린 뒤 --cleanup-only")
        else:
            n_runs, n_prof = ai_db.cleanup_range(lo, hi)
            log(f"정리: runs {n_runs} · profiles {n_prof}")
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
