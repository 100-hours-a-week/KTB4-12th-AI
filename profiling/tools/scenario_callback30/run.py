"""프로파일링 코어 30개 상품 콜백 검증 — 시나리오 엑셀(가상 수신자) → 앱 전체 → 결과 화면 + 파일.

    cd profiling && uv run python -m tools.scenario_callback30.run [--modes exclude,penalty] [--keep] [--top 5]

한 사람마다 실제 앱 경로를 그대로 탄다: 7.6 POST(TestClient) → 202 → Supervisor 워커 → pipeline.profile()(별점 편향 · 풀 30개)
→ PostgreSQL 저장(프로필 + 리뷰) → **진짜 HttpBackendPort 가 보내는 7.7 HTTP 요청**(MockTransport 가 받아 경로·토큰·본문을 기록).
서버를 따로 띄우지 않고, LLM 도 부르지 않는다. 카탈로그는 로컬 DB(`ai_catalog`, BE 번호)를 쓴다.

결과는 세 가지로 본다.
  화면  사람마다: 시간(202 · 콜백 · 서버) · 소분류 점수 · 태그 · 상위 N 과 점수 내역 · 검사 결과
  엑셀  tools/scenario_callback30/out/시나리오_결과_<시각>.xlsx — 요약 · 점수_책정 · 추천30(점수 내역) · 비선호_위치 · 태그 · 콜백_원문 · 검사 · 입력
  JSON  같은 이름 .json
검사는 두 갈래다. **자동 검사**(모든 실행): 콜백 수신 · 본문 계약 · 개수 · 카탈로그·재고 · 재현성 · 비선호 · 점수 공식 독립 재계산 ·
점수 저장 · 리뷰 행 · 원문 미저장 · 실행 기록. **시나리오 기대**: 엑셀 '기대' 시트. 하나라도 실패하면 종료 코드 1.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa

warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")   # 도구 출력에 섞이는 의존성 경고

HERE = Path(__file__).resolve().parent
DEFAULT_SCENARIOS = HERE / "scenarios.xlsx"
DEFAULT_OUT = HERE / "out"
PATH_76 = "/api/internal/v1/ai/profile/extract-and-pool"
TOKEN = "scenario-token"
RID_BASE = {"exclude": 9_300_000, "penalty": 9_310_000}
MODES = ("exclude", "penalty")


# ---------------------------------------------------------------------------
# 시나리오 읽기
# ---------------------------------------------------------------------------


@dataclass
class Expect:
    mode: str
    kind: str
    target: str
    value: str
    note: str


@dataclass
class User:
    no: str
    alias: str
    persona: str
    disliked: list[tuple[int, str]]
    preference: str | None
    note: str
    reviews: list[dict[str, Any]] = field(default_factory=list)
    expects: list[Expect] = field(default_factory=list)

    def body(self, rid: int, sv: int = 1) -> dict[str, Any]:
        return {"recipientUserId": rid, "sourceVersion": sv,
                "dislikedCategories": [{"categoryId": i, "categoryName": n} for i, n in self.disliked],
                "giftPreference": self.preference, "reviews": self.reviews}


def load_scenarios(path: Path) -> list[User]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    rows = lambda name: [r for r in wb[name].iter_rows(min_row=2, values_only=True) if r and r[0]]
    users: dict[str, User] = {}
    for no, alias, persona, disliked, pref, note in rows("사용자"):
        pairs = []
        for part in str(disliked or "").split(";"):
            if part.strip():
                cid, _, name = part.partition(":")
                pairs.append((int(cid), name.strip()))
        users[no] = User(no, alias or "", persona or "", pairs, (pref or None), note or "")
    for no, pos, pid, rating, text in sorted(rows("리뷰"), key=lambda r: (r[0], r[1])):
        users[no].reviews.append({"productId": int(pid), "rating": int(rating), "reviewText": text or None})
    for no, mode, kind, target, value, note in rows("기대"):
        users[no].expects.append(Expect(mode, kind, str(target or ""), str(value or ""), note or ""))
    return list(users.values())


# ---------------------------------------------------------------------------
# 한 모드 실행 — 앱 전체를 TestClient 로
# ---------------------------------------------------------------------------


class _Collect(logging.Handler):
    """profiling.* 경고를 수신자 번호별로 모은다 (화면·엑셀에 '서버 경고'로)."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


def run_mode(mode: str, users: list[User], engine: sa.Engine) -> list[dict[str, Any]]:
    os.environ.update({"PROFILING_CATALOG_SOURCE": "db", "PROFILING_SERVICE_TOKEN": TOKEN,
                       "PROFILING_DISLIKE_MODE": mode, "PROFILING_LOG_LEVEL": "ERROR"})
    from fastapi.testclient import TestClient

    from profiling import pipeline
    from profiling.backend import HttpBackendPort
    from profiling.settings import get_settings
    from profiling.types import DislikedCategory, ProfileRequest, Review

    get_settings.cache_clear()
    from profiling.main import app

    calls: list[dict[str, Any]] = []

    def backend_handler(request: httpx.Request) -> httpx.Response:     # 7.7 을 받는 쪽(BE 흉내) — 실제 HTTP 요청을 그대로 기록
        body = json.loads(request.content)
        calls.append({"t": time.perf_counter(), "path": request.url.path, "auth": request.headers.get("authorization"), "body": body})
        return httpx.Response(200, json={"message": "ok", "data": {"recipientUserId": body["recipientUserId"],
                                                                  "sourceVersion": body["sourceVersion"], "profileStatus": "COMPLETED"}})

    results = []
    plog = logging.getLogger("profiling")
    collector = _Collect()
    plog.addHandler(collector)
    propagate = plog.propagate
    plog.propagate = False                              # 앱 로그(시작·종료 포함)는 화면에 섞지 않고 '서버 경고'로만 모은다
    with TestClient(app) as client:
        app.state.backend = HttpBackendPort("http://backend.scenario", TOKEN, 5.0, transport=httpx.MockTransport(backend_handler))
        plog.setLevel(logging.INFO)
        catalog = app.state.catalog
        _, products = catalog.active()
        for i, u in enumerate(users, start=1):
            rid = RID_BASE[mode] + i
            body = u.body(rid)
            collector.records.clear()
            n_calls = len(calls)
            t0 = time.perf_counter()
            resp = client.post(PATH_76, json=body, headers={"Authorization": f"Bearer {TOKEN}"})
            t202 = time.perf_counter() - t0
            deadline = t0 + 15
            while len(calls) == n_calls and time.perf_counter() < deadline:
                time.sleep(0.001)
            mine = [c for c in calls[n_calls:] if c["body"].get("recipientUserId") == rid]
            t_cb = (mine[0]["t"] - t0) if mine else None
            run_row = _wait_run(engine, rid)
            app_warnings = list(collector.records)      # 앱이 낸 경고만 — 아래 하네스 계산이 같은 경고를 또 내므로 먼저 떠 둔다
            # 같은 입력으로 코드가 낼 순위(전체) — 콜백과 대조하고 점수 내역을 보여 주는 데 쓴다
            rq = ProfileRequest(recipient_user_id=rid, source_version=1, gift_preference=u.preference,
                                disliked_categories=[DislikedCategory(category_id=c, category_name=n) for c, n in u.disliked],
                                reviews=[Review(product_id=r["productId"], rating=r["rating"], review_text=r["reviewText"]) for r in u.reviews])
            scores = pipeline.category_scores(rq, products, catalog)
            ranked = pipeline.rank_products(rq, products, dislike_mode=pipeline.DislikeMode(mode), scores=scores)
            results.append({"user": u, "mode": mode, "rid": rid, "status_202": resp.status_code, "t202": t202, "t_cb": t_cb,
                            "calls": mine, "run": run_row, "scores": scores, "ranked": ranked, "products": products,
                            "warnings": app_warnings})
    plog.removeHandler(collector)
    plog.propagate = propagate
    return results


def _wait_run(engine: sa.Engine, rid: int, timeout: float = 15.0) -> dict[str, Any] | None:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        with engine.connect() as c:
            row = c.execute(sa.text("""select status, callback_attempts, callback_payload->'recommendedProductIds' as ids,
                                       extract(epoch from updated_at - created_at) as srv from ai_profile.profile_runs
                                       where recipient_user_id=:r and source_version=1"""), {"r": rid}).mappings().first()
        if row and row["status"] in ("DELIVERED", "SUPERSEDED", "FAILED"):
            return dict(row)
        time.sleep(0.002)
    return None


# ---------------------------------------------------------------------------
# 검사
# ---------------------------------------------------------------------------


def _target_ids(spec: str) -> tuple[str, set[int]]:
    level, _, ids = spec.partition(":")
    return level.strip(), {int(x) for x in ids.split(",") if x.strip()}


def _match(p, level: str, ids: set[int]) -> bool:
    return (p.categoryId in ids) if level == "소분류" else (p.parentCategoryId in ids)


def check_run(r: dict[str, Any], engine: sa.Engine, other: dict[str, Any] | None) -> list[tuple[str, str, bool, str]]:
    """(종류, 검사, 통과, 내용) 목록."""
    from pydantic import ValidationError

    from profiling.backend import CALLBACK_PATH
    from profiling.pipeline import BIAS_WEIGHT, DISLIKED_CATEGORY_SCORE
    from profiling.schemas import ProfileCallbackRequest
    from profiling.stores import DbRecipientProfileStore
    from profiling.types import Review, review_hash

    u, mode, rid = r["user"], r["mode"], r["rid"]
    by_id = {p.productId: p for p in r["products"]}
    out: list[tuple[str, str, bool, str]] = []
    add = lambda name, ok, detail="": out.append(("자동", name, bool(ok), detail))

    add("7.6 접수 202", r["status_202"] == 202, f"HTTP {r['status_202']}")
    calls = r["calls"]
    ok = len(calls) == 1 and calls[0]["path"] == CALLBACK_PATH.format(recipientUserId=rid) and calls[0]["auth"] == f"Bearer {TOKEN}"
    add("7.7 콜백 1회 · 경로 · 서비스 토큰", ok, f"{len(calls)}회 · {calls[0]['path'] if calls else '-'}")
    ids: list[int] = calls[0]["body"].get("recommendedProductIds", []) if calls else []
    try:
        cb = ProfileCallbackRequest.model_validate(calls[0]["body"]) if calls else None
        add("7.7 본문 계약(모르는 필드 없음 · 번호 일치 · 태그 없음)", cb is not None and cb.recipientUserId == rid and cb.sourceVersion == 1,
            json.dumps(calls[0]["body"], ensure_ascii=False)[:120] if calls else "")
    except ValidationError as e:
        add("7.7 본문 계약(모르는 필드 없음 · 번호 일치 · 태그 없음)", False, str(e)[:200])
    want_n = min(30, len(r["ranked"]))
    add("추천 개수 = min(30, 후보 수) · 중복 없음", len(ids) == want_n and len(set(ids)) == len(ids), f"{len(ids)}개(기대 {want_n})")
    bad = [i for i in ids if i not in by_id or by_id[i].availability == "unavailable"]
    add("전부 카탈로그에 있고 재고 없음(unavailable) 아님", not bad, f"문제 {bad[:5]}")
    run_ids = list((r["run"] or {}).get("ids") or [])
    top = [s.product.productId for s in r["ranked"][:30]]
    add("재현성: 콜백 30 = 실행 기록 30 = 같은 입력의 순위 앞 30", ids == run_ids == top, "" if ids == top else f"첫 차이 {next((k for k, (a, b) in enumerate(zip(ids, top)) if a != b), '-')}위")

    dis_ids = {c for c, _ in u.disliked}
    dis_names = {n for _, n in u.disliked}
    is_dis = lambda p: (p.categoryId in dis_ids or p.categoryName in dis_names or p.parentCategoryId in dis_ids or p.parentCategoryName in dis_names)
    flags = [is_dis(by_id[i]) for i in ids if i in by_id]
    if mode == "exclude":
        add("비선호(제외 모드): 30개에 비선호 상품 0", not any(flags), f"비선호 {sum(flags)}개")
    else:
        first_dis = next((k for k, f in enumerate(flags) if f), None)
        add("비선호(감점 모드): 비선호 상품은 비선호 아닌 상품보다 뒤에만", first_dis is None or not any(not f for f in flags[first_dis:]),
            f"30개 중 비선호 {sum(flags)}개")

    # 점수 독립 재계산 — 코드(score_categories · rank_products)와 별개로 리뷰·카탈로그에서 직접
    seen, manual = set(), {}
    for rv in u.reviews:
        if rv["productId"] in seen:
            continue
        seen.add(rv["productId"])
        p = by_id.get(rv["productId"])
        if p is None:
            continue
        n, s = manual.get(p.categoryId, (0, 0))
        manual[p.categoryId] = (n + 1, s + rv["rating"] - 3)
    got = {s.category_id: (s.n, s.score_sum) for s in r["scores"]}
    add("소분류 점수(n · 점수 합) 독립 재계산과 같음", got == manual, f"코드 {got} · 직접 {manual}")
    cand = [p for p in r["products"] if p.availability != "unavailable"]
    # 인기 = 조회수 순위 백분위 × 2 — 조회수별 개수를 세어 "나보다 낮은 후보 수"를 직접 만든다(코드의 bisect 와 다른 방법)
    counts: dict[int, int] = {}
    for p in cand:
        counts[p.viewCount] = counts.get(p.viewCount, 0) + 1
    lower, acc = {}, 0
    for v in sorted(counts):
        lower[v], acc = acc, acc + counts[v]
    span = len(cand) - 1
    worst = 0.0
    for s in r["ranked"]:
        n, sm = manual.get(s.product.categoryId, (0, 0))
        expect = (2.0 * lower[s.product.viewCount] / span if span > 0 else 0.0) + BIAS_WEIGHT * (sm / (n + 2) if n else 0.0) + (DISLIKED_CATEGORY_SCORE if mode == "penalty" and is_dis(s.product) else 0.0)
        worst = max(worst, abs(expect - s.total))
    ordered = all((a.total, -a.product.productId) >= (b.total, -b.product.productId) for a, b in zip(r["ranked"], r["ranked"][1:]))
    add("점수 공식 독립 재계산(인기 백분위 + 편향 + 감점) · 정렬", worst < 1e-9 and ordered, f"최대 오차 {worst:.2e}")

    prof = DbRecipientProfileStore(engine).get(rid)
    add("저장된 category_scores = 코드 점수", prof is not None and prof.category_scores == r["scores"], "")
    seen, want_rows = set(), []
    for pos, rv in enumerate(u.reviews):
        if rv["productId"] in seen:
            continue
        seen.add(rv["productId"])
        want_rows.append((rv["productId"], rv["rating"], pos, review_hash(Review(product_id=rv["productId"], rating=rv["rating"], review_text=rv["reviewText"]))))
    have_rows = [(x.product_id, x.rating, x.position, x.review_hash) for x in (prof.reviews if prof else [])]
    add("리뷰 행(중복 제거 · 자리 · 지문) 저장", have_rows == want_rows, f"{len(have_rows)}행(기대 {len(want_rows)})")
    texts = [rv["reviewText"] for rv in u.reviews if rv["reviewText"]] + ([u.preference] if u.preference else [])
    with engine.connect() as c:
        dump = c.execute(sa.text(
            "select coalesce((select row_to_json(p)::text from ai_profile.recipient_profiles p where p.recipient_user_id=:r),'')"
            " || coalesce((select json_agg(x)::text from ai_profile.recipient_reviews x where x.recipient_user_id=:r),'')"
            " || coalesce((select json_agg(x)::text from ai_profile.profile_runs x where x.recipient_user_id=:r),'')"), {"r": rid}).scalar()
    leaked = [t for t in texts if t in dump]
    add("원문 미저장(리뷰 글 · 취향 문장이 DB 어디에도 없음)", not leaked, f"검사 문자열 {len(texts)}개 · 발견 {len(leaked)}")
    run = r["run"] or {}
    add("실행 기록 DELIVERED · 콜백 시도 1", run.get("status") == "DELIVERED" and run.get("callback_attempts") == 1, f"{run.get('status')} · {run.get('callback_attempts')}")

    # 시나리오 기대
    for e in u.expects:
        if e.mode not in ("both", mode):
            continue
        name = f"{e.kind} {e.target} {e.value} — {e.note}".strip()
        prods = [by_id[i] for i in ids if i in by_id]
        if e.kind in ("상위N_최소", "상위N_최대"):
            level, tids = _target_ids(e.target)
            topn, k = (int(x) for x in e.value.split(":"))
            cnt = sum(1 for p in prods[:topn] if _match(p, level, tids))
            ok = cnt >= k if e.kind == "상위N_최소" else cnt <= k
            out.append(("기대", name, ok, f"상위 {topn} 중 {cnt}개"))
        elif e.kind == "조회수순":
            views = [p.viewCount for p in prods]
            out.append(("기대", name, views == sorted(views, reverse=True), f"조회수 {views[:5]}…"))
        elif e.kind == "모드간_동일":
            other_ids = other["calls"][0]["body"]["recommendedProductIds"] if other and other["calls"] else None
            out.append(("기대", name, other_ids is not None and other_ids == ids, "다른 모드 실행 없음" if other_ids is None else ""))
        elif e.kind == "순위_앞섬":
            a, _, b = e.target.partition(">")
            rank = {}
            for spec in (a, b):
                level, tids = _target_ids(spec)
                rank[spec] = next((k + 1 for k, s in enumerate(r["ranked"]) if _match(s.product, level, tids)), None)
            ok = rank[a] is not None and (rank[b] is None or rank[a] < rank[b])
            out.append(("기대", name, ok, f"{a} {rank[a]}위 · {b} {rank[b]}위 / {len(r['ranked'])}"))
        else:
            out.append(("기대", name, False, f"모르는 검사 종류 '{e.kind}'"))
    return out


# ---------------------------------------------------------------------------
# 화면 · 파일
# ---------------------------------------------------------------------------


def ms(x: float | None) -> str:
    return "-" if x is None else f"{x * 1000:.1f}ms"


def print_run(r: dict[str, Any], checks, top: int) -> None:
    u = r["user"]
    auto = [c for c in checks if c[0] == "자동"]
    exp = [c for c in checks if c[0] == "기대"]
    a_ok, e_ok = sum(c[2] for c in auto), sum(c[2] for c in exp)
    mark = "✅" if a_ok == len(auto) and e_ok == len(exp) else "❌"
    ids = r["calls"][0]["body"]["recommendedProductIds"] if r["calls"] else []
    print(f"\n{mark} [{u.no} {u.alias} · {r['mode']}] 202 {ms(r['t202'])} · 7.7 콜백까지 {ms(r['t_cb'])} · 서버 {ms((r['run'] or {}).get('srv') and float(r['run']['srv']))}"
          f" · 상품 {len(ids)}개 · 자동 {a_ok}/{len(auto)} · 기대 {e_ok}/{len(exp)}")
    if r["scores"]:
        parts = []
        for s in r["scores"]:
            p = next((x for x in r["products"] if x.categoryId == s.category_id), None)
            parts.append(f"{p.categoryName if p else s.category_id}({s.category_id}) n{s.n} 합{s.score_sum:+d} → 편향 {s.bias:+.2f}")
        print("   점수: " + " | ".join(parts))
    else:
        print("   점수: 리뷰 없음(또는 카탈로그에 없는 상품뿐) → 편향 0, 조회수순")
    dis = ", ".join(n for _, n in u.disliked) or "없음"
    print(f"   태그: 선호 [] · 비선호 [{dis}]  (글·취향 → 태그 추출(LLM)은 아직 코어에 없음)")
    for w in r["warnings"]:
        print(f"   서버 경고: {w[:140]}")
    for k, s in enumerate(r["ranked"][:top], start=1):
        p = s.product
        pen = f" {s.penalty:+.2f}" if s.penalty else ""
        print(f"   {k:>2}. #{p.productId} [{p.parentCategoryName}>{p.categoryName}] {p.name[:28]}  총 {s.total:.3f} = 인기 {s.popularity:.3f} + 편향 {s.bias:+.3f}{pen}")
    for c in checks:
        if not c[2]:
            print(f"   ❌ {c[0]} · {c[1]} — {c[3]}")


def write_files(results, checks_by, out_dir: Path, scenarios: Path) -> tuple[Path, Path]:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")   # 파일 이름은 실행한 사람의 현지 시각
    xlsx, js = out_dir / f"시나리오_결과_{stamp}.xlsx", out_dir / f"시나리오_결과_{stamp}.json"
    font, hfont, hfill = Font(name="Arial", size=10), Font(name="Arial", size=10, bold=True, color="FFFFFF"), PatternFill("solid", fgColor="007D77")
    red, green = PatternFill("solid", fgColor="F4CCCC"), PatternFill("solid", fgColor="D9EAD3")
    wb = Workbook()

    def sheet(title, head, rows, widths, first=False):
        ws = wb.active if first else wb.create_sheet(title)
        ws.title = title
        ws.append(head)
        for c in ws[1]:
            c.font, c.fill = hfont, hfill
        for row in rows:
            ws.append(row)
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.font, c.alignment = font, Alignment(vertical="top", wrap_text=isinstance(c.value, str) and len(c.value) > 40)
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        return ws

    summ = []
    for r in results:
        ch = checks_by[(r["user"].no, r["mode"])]
        auto, exp = [c for c in ch if c[0] == "자동"], [c for c in ch if c[0] == "기대"]
        ok = all(c[2] for c in ch)
        summ.append([r["user"].no, r["user"].alias, r["mode"], r["rid"], len(r["user"].reviews), len(r["scores"]),
                     len(r["calls"][0]["body"]["recommendedProductIds"]) if r["calls"] else 0,
                     round(r["t202"] * 1000, 2), round(r["t_cb"] * 1000, 2) if r["t_cb"] else None,
                     round(float((r["run"] or {}).get("srv") or 0) * 1000, 2), f"{sum(c[2] for c in auto)}/{len(auto)}",
                     f"{sum(c[2] for c in exp)}/{len(exp)}", "PASS" if ok else "FAIL", r["user"].note])
    ws = sheet("요약", ["사용자", "별칭", "모드", "수신자 번호", "리뷰 수", "점수 소분류 수", "콜백 상품 수", "202(ms)", "7.7 콜백까지(ms)",
                       "서버 처리(ms)", "자동 검사", "시나리오 기대", "결과", "보고 싶은 것"], summ, [7, 16, 9, 12, 7, 9, 9, 9, 11, 10, 9, 10, 7, 60], first=True)
    for row in ws.iter_rows(min_row=2):
        row[12].fill = green if row[12].value == "PASS" else red

    srows = []
    for r in results:
        if r["mode"] != results[0]["mode"]:
            continue
        name_of = {p.categoryId: (p.categoryName, p.parentCategoryName) for p in r["products"]}
        for s in r["scores"]:
            rv = ", ".join(f"#{x['productId']}·{x['rating']}점" for x in r["user"].reviews if x["productId"] in {p.productId for p in r["products"] if p.categoryId == s.category_id})
            srows.append([r["user"].no, r["user"].alias, s.category_id, name_of.get(s.category_id, ("", ""))[0], name_of.get(s.category_id, ("", ""))[1],
                          s.n, s.score_sum, round(s.bias, 4), rv])
        if not r["scores"]:
            srows.append([r["user"].no, r["user"].alias, None, "(점수 없음 — 편향 0)", None, 0, 0, 0.0, ""])
    sheet("점수_책정", ["사용자", "별칭", "소분류 ID", "소분류", "대분류", "n(리뷰 수)", "점수 합(별점−3)", "편향 = 합 ÷ (n+2)", "근거 리뷰"], srows,
          [7, 16, 9, 16, 12, 9, 12, 14, 40])

    rec, dis_rows = [], []
    for r in results:
        reviewed = {s.category_id for s in r["scores"]}
        for k, s in enumerate(r["ranked"][:30], start=1):
            p = s.product
            rec.append([r["user"].no, r["mode"], k, p.productId, p.name, p.brand, p.parentCategoryName, p.categoryName, p.viewCount,
                        round(s.popularity, 4), round(s.bias, 4), round(s.penalty, 4), round(s.total, 4), "예" if s.disliked else "",
                        "예" if p.categoryId in reviewed else ""])
        if r["mode"] == "penalty" and r["user"].disliked:
            best: dict[int, tuple[int, Any]] = {}
            for k, s in enumerate(r["ranked"], start=1):
                if s.disliked and s.product.categoryId not in best:
                    best[s.product.categoryId] = (k, s)
            for cid, (k, s) in sorted(best.items(), key=lambda kv: kv[1][0])[:15]:
                dis_rows.append([r["user"].no, r["user"].alias, s.product.parentCategoryName, s.product.categoryName, k, len(r["ranked"]),
                                 round(s.total, 4), round(s.bias, 4), "30위 안" if k <= 30 else "30위 밖"])
    sheet("추천30", ["사용자", "모드", "순위", "상품 ID", "상품명", "브랜드", "대분류", "소분류", "조회수", "인기(0~2)", "편향", "비선호 감점", "총점",
                    "비선호", "리뷰한 소분류"], rec, [7, 9, 6, 9, 44, 16, 12, 16, 9, 9, 8, 9, 8, 7, 9])
    sheet("비선호_위치", ["사용자", "별칭", "대분류", "소분류", "전체 순위(최고)", "후보 수", "총점", "편향", "판정"], dis_rows, [7, 16, 12, 16, 12, 9, 8, 8, 9])

    tags = [[r["user"].no, r["mode"], "[]", json.dumps([n for _, n in r["user"].disliked], ensure_ascii=False),
             "글·취향 → 태그 추출(LLM)은 아직 코어에 없음 — 비선호 이름만 disliked_tags 로", r["user"].preference or ""] for r in results]
    sheet("태그", ["사용자", "모드", "preferred_tags", "disliked_tags", "메모", "취향 문장(입력, 저장 안 함)"], tags, [7, 9, 14, 30, 50, 40])
    sheet("콜백_원문", ["사용자", "모드", "경로", "Authorization", "본문"], [[r["user"].no, r["mode"], c["path"], c["auth"], json.dumps(c["body"], ensure_ascii=False)]
                                                                         for r in results for c in r["calls"]], [7, 9, 44, 24, 120])
    crow = [[no, mode, k, n, "통과" if ok else "실패", d] for (no, mode), ch in checks_by.items() for k, n, ok, d in ch]
    ws = sheet("검사", ["사용자", "모드", "종류", "검사", "결과", "내용"], crow, [7, 9, 6, 60, 7, 70])
    for row in ws.iter_rows(min_row=2):
        row[4].fill = green if row[4].value == "통과" else red
    inp = [[u.no, u.alias, u.persona, "; ".join(f"{c}:{n}" for c, n in u.disliked), u.preference or "",
            " | ".join(f"#{x['productId']} {x['rating']}점" for x in u.reviews)] for u in {r["user"].no: r["user"] for r in results}.values()]
    sheet("입력", ["사용자", "별칭", "페르소나", "비선호", "취향 문장", f"리뷰 (원본: {scenarios.name})"], inp, [7, 16, 40, 30, 36, 60])
    wb.save(xlsx)

    js.write_text(json.dumps([{
        "user": r["user"].no, "alias": r["user"].alias, "mode": r["mode"], "recipientUserId": r["rid"],
        "timing_ms": {"accept_202": round(r["t202"] * 1000, 3), "callback": round(r["t_cb"] * 1000, 3) if r["t_cb"] else None,
                      "server": round(float((r["run"] or {}).get("srv") or 0) * 1000, 3)},
        "category_scores": [{"category_id": s.category_id, "n": s.n, "score_sum": s.score_sum, "bias": s.bias} for s in r["scores"]],
        "callback": r["calls"][0]["body"] if r["calls"] else None,
        "top30": [{"rank": k, "productId": s.product.productId, "total": s.total, "popularity": s.popularity, "bias": s.bias, "penalty": s.penalty}
                  for k, s in enumerate(r["ranked"][:30], start=1)],
        "checks": [{"kind": k, "name": n, "ok": ok, "detail": d} for k, n, ok, d in checks_by[(r["user"].no, r["mode"])]],
        "warnings": r["warnings"],
    } for r in results], ensure_ascii=False, indent=1), encoding="utf-8")
    return xlsx, js


# ---------------------------------------------------------------------------


def cleanup(engine: sa.Engine) -> None:
    with engine.begin() as c:
        for base in RID_BASE.values():
            c.execute(sa.text("delete from ai_profile.recipient_profiles where recipient_user_id between :a and :b"), {"a": base, "b": base + 9_999})
            c.execute(sa.text("delete from ai_profile.profile_runs where recipient_user_id between :a and :b"), {"a": base, "b": base + 9_999})


def run(scenarios: Path = DEFAULT_SCENARIOS, out_dir: Path = DEFAULT_OUT, modes: tuple[str, ...] = MODES, *, keep: bool = False,
        top: int = 5, quiet: bool = False) -> dict[str, Any]:
    """시나리오 전체 실행 → {'ok', 'xlsx', 'json', 'results', 'checks'}. pytest(tests/integration/test_scenario_callback30.py)도 이걸 부른다."""
    from profiling.settings import get_settings

    users = load_scenarios(scenarios)
    engine = sa.create_engine(get_settings().DATABASE_URL, future=True)
    cleanup(engine)
    t_all = time.perf_counter()
    results: list[dict[str, Any]] = []
    try:
        for mode in modes:
            results += run_mode(mode, users, engine)
        by_key = {(r["user"].no, r["mode"]): r for r in results}
        checks_by = {}
        for r in results:
            other = by_key.get((r["user"].no, "exclude" if r["mode"] == "penalty" else "penalty"))
            checks_by[(r["user"].no, r["mode"])] = check_run(r, engine, other)
        if not quiet:
            print(f"\n=== 프로파일링 코어 30개 상품 콜백 검증 — 사용자 {len(users)}명 × 모드 {len(modes)} ({', '.join(modes)}) · {scenarios.name} ===")
            for r in results:
                print_run(r, checks_by[(r["user"].no, r["mode"])], top)
        xlsx, js = write_files(results, checks_by, out_dir, scenarios)
    finally:
        if not keep:
            cleanup(engine)
        engine.dispose()
    allc = [c for ch in checks_by.values() for c in ch]
    ok = all(c[2] for c in allc)
    if not quiet:
        cb = sorted(r["t_cb"] for r in results if r["t_cb"])
        print(f"\n{'✅ 전부 통과' if ok else '❌ 실패 있음'} — 검사 {sum(c[2] for c in allc)}/{len(allc)} · 실행 {len(results)}건 · "
              f"7.7 콜백까지 p90 {ms(cb[int(0.9 * (len(cb) - 1))]) if cb else '-'} · p95 {ms(cb[int(0.95 * (len(cb) - 1))]) if cb else '-'} · "
              f"최대 {ms(cb[-1]) if cb else '-'} · 전체 {time.perf_counter() - t_all:.1f}s")
        print(f"결과 파일: {xlsx}\n          {js}")
    return {"ok": ok, "xlsx": xlsx, "json": js, "results": results, "checks": checks_by}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--modes", default=",".join(MODES), help="exclude,penalty 중 쉼표로")
    ap.add_argument("--keep", action="store_true", help="실행 뒤 시험 수신자 행을 지우지 않음(DB 로 직접 보기)")
    ap.add_argument("--top", type=int, default=5, help="화면에 보일 상위 상품 수")
    a = ap.parse_args()
    modes = tuple(m.strip() for m in a.modes.split(",") if m.strip())
    return 0 if run(a.scenarios, a.out, modes, keep=a.keep, top=a.top)["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
