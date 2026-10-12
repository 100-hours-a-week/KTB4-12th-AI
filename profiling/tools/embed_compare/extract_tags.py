"""로컬 LLM(Ollama)으로 시나리오 사용자의 선호 태그를 뽑아 '태그' 시트에 그 모델 이름으로 적는다 — Claude 수작업 태그와 비교용.

    cd profiling && uv run python -m tools.embed_compare.extract_tags --models gemma4:12b,qwen3.5:9b
    uv run python -m tools.embed_compare.run --tagger gemma4:12b
    uv run python -m tools.embed_compare.extract_tags --from-json out/claude_prompt_responses.json   # 같은 프롬프트에 대한 다른 모델의 원출력 넣기

입력은 실제 서비스에서 AI 가 받는 것만 쓴다: 취향 문장(giftPreference) · 리뷰(별점 · 글) + 리뷰 상품의 카탈로그 정보(이름 · 브랜드 ·
대분류 > 소분류 · product_kind). 페르소나 · '보고 싶은 것'(시나리오 설명)은 넣지 않는다. 비선호는 코드가 따로 처리하므로 넣지 않는다.
같은 상품 중복 리뷰는 앞(최신)만, 카탈로그에 없는 상품은 빼고 넘긴다(코어 규칙과 같음).

규칙은 팀 폴더 '태그추출_프롬프트_유의사항.md' 1 · 2 · 3 · 6절(판촉 단어 · 증정품 · 구매 경험 · 4축)을 옮겼다. 결정성: temperature 0 · seed 고정 ·
생각 모드 끔 · JSON 스키마 강제. 검증기: 축이 4개 중 하나 · 근거 ref(여러 개 가능)가 실제 리뷰/취향 · 근거가 별점 1~3 리뷰뿐이면 버림 · 판촉 단어 태그는 버림.
기록: out/태그추출_<모델>_<시각>.json(프롬프트 해시 · 모델 digest · 사람마다 시간 · 원출력 · 버린 태그와 사유).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa

from tools.embed_compare.run import DEFAULT_OUT, DEFAULT_SCENARIOS

OLLAMA = "http://localhost:11434"
AXES = ("카테고리", "IP", "브랜드", "특징")
PROMO = re.compile(r"증정|사은품|선물포장|쇼핑백|메세지카드|택1|택일|랜덤|단독|각인|NEW|신상|한정|리뉴얼|에디션|BEST|베스트|1위|인기|누적판매|"
                   r"백화점|공식|정품|본사|당일발송|빠른배송|무료배송|추천", re.IGNORECASE)
SEED = 42

SYSTEM = """너는 선물 추천 서비스의 취향 태그 추출기다. 선물 받을 사람이 남긴 리뷰와 취향 문장을 읽고, 그 사람이 **좋아하는 것**을 태그로 뽑는다.

축(axis)은 네 가지 중 하나만 쓴다.
- 카테고리: 상품 종류(예: 향수, 헤드폰, 케이크, 출산 선물세트). 가능하면 리뷰 상품의 소분류나 상품 종류(kind) 수준으로.
- IP: 캐릭터 · 지식재산(예: 산리오 한교동, 카카오프렌즈 춘식이). 캐릭터는 IP 와 함께 적는다.
- 브랜드: 상품의 브랜드(예: 소니, 조말론런던). 판매처 · 백화점은 브랜드가 아니다.
- 특징: 향 · 맛 · 소재 · 기능 · 질감 같은 성질(예: 은은한 향, 노이즈캔슬링, 부드러운 소재).

규칙
1. 별점 4~5점 리뷰와 취향 문장만 근거로 쓴다. 별점 1~3점 리뷰로는 선호 태그를 만들지 않는다.
2. 상품명의 판촉 문구는 상품의 특징이 아니라 판매 조건이다. 태그로도 근거로도 쓰지 마라:
   증정 · 사은품, 단독, 선물포장 · 쇼핑백 · 메세지카드, 택1 · N종 택1 · 랜덤, 각인, NEW · 신상 · 한정 · 리뉴얼 · 에디션,
   BEST · 1위 · 인기 · 누적판매, 백화점 · 공식 · 정품 · 본사, 당일발송 · 빠른배송, 추천. 상품명 맨 앞 [ ] 머리말과 따옴표 광고 문구는 대부분 판촉이다.
3. "(+○○ 증정)", "[○○ 증정]" 같은 증정품은 본품이 아니다. 그 이름 · 종류 · 캐릭터로 태그를 만들지 마라.
4. 리뷰 글의 포장 · 배송 · 증정품 · 가격 이야기는 취향이 아니다.
5. 같은 말이 여러 축에 걸리면 더 구체적인 축 하나에만 둔다(예: 포켓몬 → IP).
6. 태그는 짧은 명사구로, 사람마다 2~6개. 근거가 없으면 빈 목록을 낸다.
7. ref 는 근거가 된 리뷰 번호(r1, r2 …) 또는 취향 문장이면 pref 다."""

SCHEMA = {"type": "object", "properties": {"tags": {"type": "array", "items": {
    "type": "object", "properties": {"axis": {"type": "string", "enum": list(AXES)}, "tag": {"type": "string"},
                                     "ref": {"type": "string"}, "evidence": {"type": "string"}},
    "required": ["axis", "tag", "ref", "evidence"]}}}, "required": ["tags"]}


def user_prompt(u, info: dict[int, dict[str, Any]]) -> tuple[str, dict[str, int]]:
    """사람 하나의 입력 — (프롬프트, ref → 별점)."""
    lines, ratings, seen = [], {}, set()
    for rv in u.reviews:
        pid = rv["productId"]
        if pid in seen or pid not in info:
            continue
        seen.add(pid)
        p = info[pid]
        ref = f"r{len(ratings) + 1}"
        ratings[ref] = rv["rating"]
        lines.append(f"{ref}. 별점 {rv['rating']}점 · 상품명: {p['name']} · 브랜드: {p['brand']} · 분류: {p['path']} · 종류: {p['kind']}"
                     f" · 리뷰 글: {rv['reviewText'] or '(없음)'}")
    pref = u.preference or "(없음)"
    body = f"취향 문장(pref): {pref}\n리뷰:\n" + ("\n".join(lines) if lines else "(없음)")
    return body + '\n\nJSON 으로만 답하라: {"tags": [{"axis": ..., "tag": ..., "ref": ..., "evidence": ...}]}', ratings


def validate(tags: list[dict[str, Any]], ratings: dict[str, int], has_pref: bool) -> tuple[list[dict], list[dict]]:
    """근거 ref 는 여러 개일 수 있다("pref, r1") — 하나라도 취향 문장이거나 4~5점 리뷰면 통과, 전부 1~3점이거나 없는 번호면 버림."""
    keep, drop = [], []
    for t in tags:
        tag = str(t.get("tag", "")).strip()
        refs = [x for x in re.split(r"[\s,/·]+", str(t.get("ref", ""))) if x]
        known = [x for x in refs if (x == "pref" and has_pref) or x in ratings]
        good = [x for x in known if x == "pref" or ratings[x] >= 4]
        why = ("축이 4개 밖" if t.get("axis") not in AXES else "빈 태그" if not tag else "판촉 단어" if PROMO.search(tag)
               else "근거 ref 가 없음" if not known
               else "근거가 별점 1~3점 리뷰뿐(" + ",".join(f"{x}={ratings[x]}" for x in known) + ")" if not good else "")
        (drop if why else keep).append({**t, "tag": tag, **({"drop": why} if why else {})})
    uniq, seen = [], set()
    for t in keep:                                  # 같은 축 · 같은 태그는 하나만
        if (t["axis"], t["tag"]) not in seen:
            seen.add((t["axis"], t["tag"]))
            uniq.append(t)
    return uniq, drop


def extract(model: str, users, info) -> dict[str, Any]:
    digest = next((m["digest"] for m in httpx.get(f"{OLLAMA}/api/tags", timeout=10).json()["models"] if m["name"] == model), None)
    if digest is None:
        raise SystemExit(f"Ollama 에 {model} 이 없음 — ollama pull {model}")
    out = {"model": model, "digest": digest, "seed": SEED, "prompt_sha256": hashlib.sha256((SYSTEM + json.dumps(SCHEMA)).encode()).hexdigest(),
           "users": {}}
    with httpx.Client(base_url=OLLAMA, timeout=600) as c:
        c.post("/api/generate", json={"model": model, "prompt": "", "keep_alive": "10m"})          # 모델 올리기(시간 측정에서 뺌)
        for u in users:
            prompt, ratings = user_prompt(u, info)
            t0 = time.perf_counter()
            r = c.post("/api/chat", json={"model": model, "stream": False, "think": False, "format": SCHEMA,
                                          "options": {"temperature": 0, "seed": SEED},
                                          "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]})
            dt = time.perf_counter() - t0
            r.raise_for_status()
            body = r.json()
            raw = body["message"]["content"]
            try:
                tags = json.loads(raw).get("tags", [])
            except json.JSONDecodeError:
                tags = []
            keep, drop = validate(tags, ratings, bool(u.preference))
            out["users"][u.no] = {"seconds": round(dt, 3), "eval_tokens": body.get("eval_count"), "prompt": prompt, "raw": raw,
                                  "tags": keep, "dropped": drop}
            print(f"  {model} {u.no} {dt:5.1f}s · 태그 {len(keep)} · 버림 {len(drop)} | " + " · ".join(f"{t['axis']}:{t['tag']}" for t in keep))
    return out


def from_responses(path: Path, users, info) -> dict[str, Any]:
    """Ollama 밖에서 같은 SYSTEM · user_prompt 로 받은 원출력(JSON)을 같은 검증기로 — 예: claude-opus-5-5 가 대화 안에서 답한 것.
    파일 모양: {"model": 이름, "note": 설명, "responses": {"U01": {"tags": [...]}, ...}}. 시간은 없다(API 호출이 아님)."""
    doc = json.loads(path.read_text())
    out = {"model": doc["model"], "digest": "", "seed": None, "note": doc.get("note", ""),
           "prompt_sha256": hashlib.sha256((SYSTEM + json.dumps(SCHEMA)).encode()).hexdigest(), "users": {}}
    for u in users:
        prompt, ratings = user_prompt(u, info)
        raw = doc["responses"].get(u.no, {"tags": []})
        keep, drop = validate(raw.get("tags", []), ratings, bool(u.preference))
        out["users"][u.no] = {"seconds": None, "eval_tokens": None, "prompt": prompt, "raw": json.dumps(raw, ensure_ascii=False),
                              "tags": keep, "dropped": drop}
        print(f"  {doc['model']} {u.no} 태그 {len(keep)} · 버림 {len(drop)} | " + " · ".join(f"{t['axis']}:{t['tag']}" for t in keep)
              + ("".join(f" ✗{t['tag']}({t['drop']})" for t in drop)))
    return out


def write_sheet(path: Path, model: str, res: dict[str, Any]) -> None:
    """'태그' 시트에서 그 모델 행을 지우고 새로 적는다. 다른 모델(claude-opus-5-5 등) 행은 건드리지 않는다."""
    from openpyxl import load_workbook

    wb = load_workbook(path)
    ws = wb["태그"]
    for r in range(ws.max_row, 1, -1):
        if ws.cell(r, 5).value == model:
            ws.delete_rows(r)
    how = (f"tools/embed_compare/extract_tags.py · Ollama · temperature 0 · seed {SEED} · 생각 끔 · 프롬프트 {res['prompt_sha256'][:8]} · {datetime.now().astimezone():%m-%d}"
           if res.get("seed") is not None else f"같은 프롬프트 {res['prompt_sha256'][:8]} · {res.get('note', '')[:80]} · {datetime.now().astimezone():%m-%d}")
    for no, u in res["users"].items():
        for t in u["tags"]:
            ws.append((no, t["axis"], t["tag"], f"{t['ref']}: {t['evidence']}"[:120], model, how))
    wb.save(path)


def main() -> int:
    from profiling.settings import get_settings
    from tools.scenario_callback30.run import load_scenarios

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models", default="gemma4:12b,qwen3.5:9b")
    ap.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    ap.add_argument("--from-json", type=Path, help="Ollama 대신 원출력 파일(같은 프롬프트에 대한 다른 모델의 답)을 검증해 시트에 넣음")
    a = ap.parse_args()
    users = load_scenarios(a.scenarios)
    eng = sa.create_engine(get_settings().DATABASE_URL, future=True)
    with eng.connect() as c:
        info = {r.id: {"name": r.name, "brand": r.brand, "path": f"{r.parent} > {r.cat}", "kind": r.kind} for r in c.execute(sa.text(
            "select p.backend_product_id id, p.name, p.brand, p.product_kind kind, c.name cat, cp.name parent from ai_catalog.products p "
            "join ai_catalog.categories c on c.source_category_id = p.source_category_id "
            "left join ai_catalog.categories cp on cp.source_category_id = c.parent_source_category_id where p.backend_product_id is not null"))}
    eng.dispose()
    DEFAULT_OUT.mkdir(parents=True, exist_ok=True)
    if a.from_json:
        res = from_responses(a.from_json, users, info)
        write_sheet(a.scenarios, res["model"], res)
        log = DEFAULT_OUT / f"태그추출_{re.sub(r'[^\w.-]', '_', res['model'])}_{datetime.now().astimezone():%Y%m%d_%H%M%S}.json"
        log.write_text(json.dumps(res, ensure_ascii=False, indent=1))
        print(f"  → 태그 {sum(len(u['tags']) for u in res['users'].values())} · 버림 {sum(len(u['dropped']) for u in res['users'].values())} · 기록 {log.name}")
        return 0
    for model in (m.strip() for m in a.models.split(",") if m.strip()):
        print(f"\n=== 태그 추출 — {model}")
        res = extract(model, users, info)
        write_sheet(a.scenarios, model, res)
        log = DEFAULT_OUT / f"태그추출_{re.sub(r'[^\w.-]', '_', model)}_{datetime.now().astimezone():%Y%m%d_%H%M%S}.json"
        log.write_text(json.dumps(res, ensure_ascii=False, indent=1))
        secs = sorted(u["seconds"] for u in res["users"].values())
        print(f"  → 사람당 p90 {secs[int(0.9 * (len(secs) - 1))]:.1f}s · 최대 {secs[-1]:.1f}s · 태그 {sum(len(u['tags']) for u in res['users'].values())} · "
              f"버림 {sum(len(u['dropped']) for u in res['users'].values())} · 기록 {log.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
