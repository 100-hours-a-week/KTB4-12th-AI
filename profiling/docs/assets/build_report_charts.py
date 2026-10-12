"""시험 결과 보고서 그래프 — docs/프로파일링_코어_시험_결과보고서_2026-10-12.md 의 docs/assets/report/*.png

    cd profiling && uv run pytest -q --junitxml=/tmp/junit.xml
    uv run --with matplotlib python docs/assets/build_report_charts.py /tmp/junit.xml <시나리오_결과.json> <임베딩_비교.json>

원자료: pytest JUnit · tools/scenario_callback30/out/*.json · tools/embed_compare/out/*.json. 훑기 · 시뮬레이션 수치는 결과 문서 표를 옮겨 적음."""
import collections
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams.update({"font.family": "Apple SD Gothic Neo", "font.size": 11, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.unicode_minus": False, "figure.dpi": 150})
P = Path("/Users/emet/KTB4-12th/KTB4-12th-AI/profiling")
OUT = P / "docs/assets/report"; OUT.mkdir(parents=True, exist_ok=True)
JUNIT, SCEN, EMB = sys.argv[1], sys.argv[2], sys.argv[3]
TEAL, GRAY, LIGHT, ORANGE, RED = "#007D77", "#9AA0A6", "#C9D6DF", "#E8A33D", "#C0392B"
COL = {"S0": GRAY, "A": "#B7C9D3", "B": "#6C8EA4", "C": ORANGE, "D": TEAL}
def save(fig, name):
    fig.tight_layout(); fig.savefig(OUT / name, bbox_inches="tight"); plt.close(fig); print("ok", name)

# 1 시험 구성
area = {"파이프라인·점수": ["test_pipeline", "test_recipient_profile", "test_embed_compare"],
        "수신·워커·콜백": ["test_intake_dedupe", "test_supervisor", "test_callback_retry", "test_backend", "test_fake_backend_lifecycle"],
        "저장(DB)": ["test_db_stores", "test_db_recipient_profiles", "test_stores_lock_key"],
        "카탈로그": ["test_catalog", "test_catalog_fixture", "test_db_catalog", "test_db_catalog_reader", "test_load_catalog", "test_import_be_ids", "test_fetch_export", "test_fetch_export_db"],
        "계약·설정": ["test_schemas", "test_settings_catalog_poll_ttl", "test_settings_dislike_mode", "test_settings_slots_env"],
        "앱 전체·하네스": ["test_e2e_app", "test_scenario_callback30", "test_loadtest_summary"]}
c = collections.defaultdict(lambda: [0, 0, 0])
for tc in ET.parse(JUNIT).iter("testcase"):
    f = tc.get("classname").split(".")[-1]
    st = 2 if tc.find("skipped") is not None else (1 if (tc.find("failure") is not None or tc.find("error") is not None) else 0)
    c[f][st] += 1
names = list(area); vals = np.array([[sum(c[f][k] for f in area[a]) for k in range(3)] for a in names])
fig, ax = plt.subplots(figsize=(8, 3.6))
y = np.arange(len(names))[::-1]
ax.barh(y, vals[:, 0], color=TEAL, label="통과"); ax.barh(y, vals[:, 1], left=vals[:, 0], color=RED, label="실패")
ax.barh(y, vals[:, 2], left=vals[:, 0] + vals[:, 1], color=LIGHT, label="건너뜀")
for yi, v in zip(y, vals): ax.text(v.sum() + 0.8, yi, f"{v[0]}" + (f" · 실패 {v[1]}" if v[1] else "") + (f" · 건너뜀 {v[2]}" if v[2] else ""), va="center", fontsize=9)
ax.set_yticks(y, names); ax.set_xlabel("시험 수"); ax.set_title(f"자동 시험 {vals.sum()}개 — 통과 {vals[:,0].sum()} · 실패 {vals[:,1].sum()} · 건너뜀 {vals[:,2].sum()}")
ax.legend(loc="lower right", frameon=False, fontsize=9); ax.set_xlim(0, vals.sum(1).max() + 14)
save(fig, "01-시험-구성.png")

# 2 · 3 · 11 시간 백분위(p75 · p90 · p95 · p99, p50 없음) — 다섯 번째 인자 = tools.bench.percentiles 결과 JSON
PCT = json.loads(Path(sys.argv[5]).read_text()) if len(sys.argv) > 5 else None
PS, PCOL = (75, 90, 95, 99), ["#B7D7D5", "#5FA8A3", TEAL, "#08403D"]


def pbars(ax, groups, title, unit="ms", log=False):
    """groups = [(이름, 표본)] — 이름마다 p75 · p90 · p95 · p99 막대."""
    w = 0.2
    for k, p in enumerate(PS):
        vals = [float(np.percentile(v, p)) for _, v in groups]
        x = np.arange(len(groups)) + (k - 1.5) * w
        ax.bar(x, vals, w, color=PCOL[k], label=f"p{p}")
        for xi, v in zip(x, vals):
            ax.text(xi, v * (1.05 if log else 1) + (0 if log else max(1e-9, ax.get_ylim()[1]) * 0), f"{v:.1f}" if v < 100 else f"{v:.0f}",
                    ha="center", va="bottom", fontsize=6.5, rotation=90)
    ax.set_xticks(np.arange(len(groups)), [f"{g}\n(n={len(v)})" for g, v in groups], fontsize=8)
    ax.set_ylabel(unit); ax.set_title(title, fontsize=10)
    if log:
        ax.set_yscale("log")
    ax.margins(y=0.25)


if PCT:
    ao = PCT["app_only"]
    fig, axs = plt.subplots(1, 2, figsize=(11, 3.8))
    pbars(axs[0], [("반영 전\n(freeze 없음)", ao["before"]["server"]), ("반영 후\n(gc.freeze)", ao["after"]["server"]), ("GC 끔(대조)", ao["off"]["server"]),
                   ("콜백30 하네스\n(반영 후)", [r["server"] for r in PCT["callback30"]])], "서버 처리(7.6 접수 → 7.7 전송 완료)")
    pbars(axs[1], [("반영 전\n(freeze 없음)", ao["before"]["callback"]), ("반영 후\n(gc.freeze)", ao["after"]["callback"]), ("GC 끔(대조)", ao["off"]["callback"]),
                   ("콜백30 하네스\n(반영 후)", [r["callback"] for r in PCT["callback30"]])], "클라이언트: 7.6 POST → 7.7 수신")
    axs[0].legend(frameon=False, fontsize=8, ncol=4, loc="upper left")
    save(fig, "02-시간-백분위.png")
    fig, ax = plt.subplots(figsize=(7, 3.4))
    pbars(ax, list(PCT["store"].items()), "저장 한 번(리뷰 10개, 1,000회씩)")
    ax.legend(frameon=False, fontsize=8, ncol=4, loc="upper left")
    save(fig, "03-저장-방식.png")
    fig, axs = plt.subplots(1, 2, figsize=(11, 3.6), gridspec_kw={"width_ratios": [2, 1.4]})
    pbars(axs[0], [("질의 임베딩\n(태그 1개)", PCT["embed_query"]), ("D 순위 계산\n(사람 1명)", PCT["d_rank"]),
                   ("서버 처리 S0\n(반영 후)", ao["after"]["server"])], "임베딩 경로 시간")
    pbars(axs[1], [(m, v) for m, v in PCT["llm"].items()], "로컬 LLM 태그 추출(사람 1명)", unit="초")
    axs[0].legend(frameon=False, fontsize=8, ncol=4, loc="upper left")
    save(fig, "11-임베딩-LLM-시간.png")

# 4 S0 독차지 — 가중치 vs 상한 (6명 평균)
W = [1.0, 0.3, 0.1, 0.03, 0.01]
wb = np.array([[30, 12, 3, 1, 0], [30, 30, 20, 8, 6], [30, 12, 5, 2, 1], [30, 30, 22, 9, 5], [30, 27, 10, 6, 4], [30, 30, 11, 4, 2]])
Ks = [30, 10, 5, 3]
kb = np.array([[30, 20, 10, 6], [30, 20, 10, 6], [30, 10, 5, 3], [30, 30, 15, 9], [30, 20, 10, 6], [30, 10, 5, 3]])
fig, axs = plt.subplots(1, 2, figsize=(9, 3.3), sharey=True)
for row, u in zip(wb, ["U01", "U02", "U03", "U04", "U05", "U10"]): axs[0].plot(range(len(W)), row, color=LIGHT, lw=1)
axs[0].plot(range(len(W)), wb.mean(0), color=ORANGE, lw=2.5, marker="o", label="6명 평균"); axs[0].set_xticks(range(len(W)), [str(w) for w in W])
axs[0].set_xlabel("BIAS_WEIGHT"); axs[0].set_ylabel("30개 중 편향 소분류 상품"); axs[0].set_title("가중치 줄이기 — 사람마다 들쭉날쭉"); axs[0].legend(frameon=False, fontsize=9)
for row in kb: axs[1].plot(range(len(Ks)), row, color=LIGHT, lw=1)
axs[1].plot(range(len(Ks)), kb.mean(0), color=TEAL, lw=2.5, marker="o", label="6명 평균"); axs[1].set_xticks(range(len(Ks)), [str(k) for k in Ks])
axs[1].set_xlabel("소분류 상한 K"); axs[1].set_title("소분류 상한 — 예측 가능"); axs[1].legend(frameon=False, fontsize=9)
save(fig, "04-독차지-가중치-상한.png")

# 5,6,8 임베딩 비교
E = json.loads(Path(EMB).read_text())["runs"]
pen = {r["user"]: r["metrics"] for r in E if r["mode"] == "penalty"}
tagged = ["U01", "U02", "U03", "U04", "U05", "U06", "U09", "U10"]
fig, ax = plt.subplots(figsize=(9, 3.6))
strat = ["S0", "C", "D"]; lbl = {"S0": "S0 지금 점수식", "C": "C 임베딩+점수+MMR(1차)", "D": "D 배제 후 검색 · λ0.7 · K10(확정)"}
w = 0.27
for k, s in enumerate(strat):
    x = np.arange(len(tagged)) + (k - 1) * w
    ax.bar(x, [pen[u][s]["소분류_종수"] for u in tagged], w, color=COL[s], label=lbl[s])
    ax.scatter(x, [pen[u][s]["최다_개수"] for u in tagged], marker="_", s=120, color="black", zorder=3, label="최다 소분류 개수" if k == 2 else None)
ax.set_xticks(np.arange(len(tagged)), tagged); ax.set_ylabel("30개 안 소분류 종수"); ax.set_title("30개 섞임 — 소분류 종수(막대)와 한 소분류 최다 개수(가로선), 감점 모드")
ax.legend(frameon=False, fontsize=8, ncol=2, loc="upper left"); ax.set_ylim(0, 33)
save(fig, "05-섞임-비교.png")

fig, ax = plt.subplots(figsize=(6.5, 2.8))
ss = ["S0", "A", "B", "C", "D"]; v = [pen["U09"][s]["비선호"] for s in ss]
ax.bar(ss, v, color=[RED if x else COL[s] for s, x in zip(ss, v)])
for i, x in enumerate(v): ax.text(i, x + 0.2, str(x), ha="center")
ax.set_ylabel("30개 중 비선호(뷰티) 상품"); ax.set_title("U09 — 뷰티 비선호인데 향수 5점: 비선호가 새는가")
save(fig, "06-U09-비선호.png")

fig, ax = plt.subplots(figsize=(6.5, 4))
for s in ["S0", "A", "B", "C", "D"]:
    xs = np.mean([pen[u][s]["소분류_종수"] for u in tagged]); ys = np.mean([pen[u][s]["조회수_백분위_평균"] for u in tagged])
    ax.scatter(xs, ys, s=160, color=COL[s], edgecolor="black", zorder=3); ax.annotate(s, (xs, ys), xytext=(7, 5), textcoords="offset points")
ax.set_xlabel("소분류 종수 평균(섞임)"); ax.set_ylabel("조회수 백분위 평균(인기)"); ax.set_title("방식별 섞임 vs 인기 — 태그 있는 8명 평균")
ax.set_ylim(0.4, 1.02)
save(fig, "08-섞임-인기.png")

# 7 매개변수 훑기 — 섞임 vs 취향 적중
pts = {"λ0.5 · 인기0": (10.0, 30.0), "λ0.5 · 인기30": (10.0, 26.4), "λ0.5 · 인기100": (10.0, 22.2), "λ0.7 · 인기0": (7.9, 30.0),
       "λ1.0 · 인기0": (6.5, 30.0), "λ0.7 · K5": (14.4, 27.8), "λ0.7 · K10 (확정)": (11.2, 29.0), "λ0.7 · K15": (8.6, 29.9)}
fig, ax = plt.subplots(figsize=(7, 4))
for k, (x, y) in pts.items():
    col = TEAL if "확정" in k else (ORANGE if "K" in k else GRAY)
    ax.scatter(x, y, s=120 if "확정" in k else 60, color=col, edgecolor="black", zorder=3)
    off = {"λ0.7 · 인기0": (-10, 8), "λ0.7 · K15": (4, -14), "λ0.5 · 인기30": (6, -12), "λ1.0 · 인기0": (-10, 8)}.get(k, (6, 4))
    ax.annotate(k, (x, y), xytext=off, textcoords="offset points", fontsize=8.5)
ax.set_xlabel("소분류 종수 평균(섞임)"); ax.set_ylabel("취향 적중 평균(/30)"); ax.set_title("D 매개변수 훑기 — 오른쪽 위가 좋다")
ax.set_xlim(5, 16.5); ax.set_ylim(21, 31)
save(fig, "07-D-훑기.png")

# 9 · 10 태그 추출 모델 비교 (선택: 네 번째 인자 = out/태그추출_비교_*.json)
if len(sys.argv) > 4:
    T = json.loads(Path(sys.argv[4]).read_text())
    sm = T["summary"]; names = [s["tagger"] for s in sm]
    tc = {"claude-opus-5-5": "#B8B0F0", "claude-opus-5-5-prompt": "#6C5CE7", "gemma4:12b": TEAL, "qwen3.5:9b": ORANGE}
    fig, axs = plt.subplots(1, 3, figsize=(13, 3.5))
    ex = [int(s["exact"].split("/")[0]) for s in sm]; base_n = int(sm[0]["exact"].split("/")[1])
    axs[0].bar(names, [s["tags"] for s in sm], color=[tc.get(n, GRAY) for n in names])
    for i, s in enumerate(sm): axs[0].text(i, s["tags"] + 0.5, str(s["tags"]), ha="center", fontsize=9)
    axs[0].set_title("태그 수(10명 합)"); axs[0].tick_params(axis="x", labelsize=7, rotation=15)
    axs[1].bar(names, [e / base_n * 100 for e in ex], color=[tc.get(n, GRAY) for n in names])
    for i, e in enumerate(ex): axs[1].text(i, e / base_n * 100 + 2, f"{e}/{base_n}", ha="center", fontsize=9)
    axs[1].set_title(f"{names[0]} 태그와 일치(%)", fontsize=10); axs[1].set_ylim(0, 115); axs[1].tick_params(axis="x", labelsize=7, rotation=15)
    llm = PCT["llm"] if PCT else {}
    w = 0.25
    for k, p in enumerate((75, 90, 95)):
        vals = [float(np.percentile(llm[n], p)) if n in llm else 0 for n in names]
        x = np.arange(len(names)) + (k - 1) * w
        axs[2].bar(x, vals, w, color=PCOL[k], label=f"p{p}")
        for xi, v in zip(x, vals):
            if v:
                axs[2].text(xi, v + 0.3, f"{v:.1f}", ha="center", fontsize=7)
    axs[2].set_xticks(np.arange(len(names)), names)
    for i, n in enumerate(names):
        if n not in llm:
            axs[2].text(i, 0.5, "시간 없음\n(대화 안)", ha="center", fontsize=7)
    axs[2].set_title("사람당 추출 시간(초) p75 · p90 · p95"); axs[2].tick_params(axis="x", labelsize=7, rotation=15); axs[2].legend(frameon=False, fontsize=7)
    save(fig, "09-태그추출-모델.png")
    rec = T["recommend"]; us = sorted({r[1] for r in rec}); us = [u for u in us if u not in ("U07", "U08")]
    fig, ax = plt.subplots(figsize=(10, 3.4)); w = 0.8 / len(names)
    for k, n in enumerate(names):
        x = np.arange(len(us)) + (k - (len(names) - 1) / 2) * w
        ax.bar(x, [next(r[8] for r in rec if r[0] == n and r[1] == u) for u in us], w, color=tc.get(n, GRAY), label=n)
    ax.set_xticks(np.arange(len(us)), us); ax.set_ylabel(f"{names[0]} 기준 D 30개와 겹침"); ax.set_ylim(0, 33)
    ax.set_title("태그 추출 모델이 바뀌면 30개가 얼마나 바뀌나(D · 감점 모드)"); ax.legend(frameon=False, fontsize=8, ncol=len(names), loc="upper center", bbox_to_anchor=(0.5, -0.1))
    save(fig, "10-태그추출-추천-겹침.png")
