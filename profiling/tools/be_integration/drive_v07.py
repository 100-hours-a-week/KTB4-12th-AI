"""BE develop(2026-10-08, PR #260·#263 = 통합 수정점 v0.7 반영) ↔ AI 실물 시험 드라이버 — 단계별 부속 명령.

서버는 바깥(Claude 미리보기 launch.json: be-spring · profiling-app-be · profiling-app-be-badcb · profiling-app-be-badtoken)에서 켜고 끈다.
이 스크립트는 사용자 행동(로그인 · 비선호 저장)과 관찰(BE MySQL · AI Postgres)만 한다. **profiling/ 디렉터리에서**:
  uv run python tools/be_integration/drive_v07.py <명령> [--out 결과.json]

명령(순서대로). 괄호는 그때 떠 있어야 하는 AI:
  reset        수신자 1·2 의 BE 행(추천 · 프로파일 · 비선호)과 AI 행(실행 기록 · 프로필) 삭제. 서버 켜기 전에.
  s1           정상 왕복 — 사용자1 비선호 [1] → 7.6 v1 → 202 → 7.7 200 → BE COMPLETED.                         (AI 정상 :8000)
  s2-observe   AI 꺼진 채 사용자1 [1,2] → 40초 관찰: 번호가 안 올라야 한다(①③ 핑이 틱을 건너뜀, 변경 시각 유지).  (AI 꺼짐)
  s2-recover   AI 켠 뒤 → 다음 틱에 새 번호 한 건 → DELIVERED · COMPLETED.                                     (AI 정상)
  s34-start    콜백 불통 AI 아래 사용자2 [2] → v1 PENDING(AI RESULT_READY) → 이어서 [2,3] → ⑧ PENDING 중 새 번호 v2
               → ⑤ maximum-window(60s) 뒤 BE 복구 전송(같은 번호 v2) 관찰.                                       (AI badcb :8089 콜백)
  s34-finish   정상 AI 로 바꾼 뒤 → 다음 복구 전송 → AI 재전송(재분석 없음) → 7.7 200 → COMPLETED v2.             (AI 정상)
  s5-start     토큰 틀린 AI 아래 사용자1 [3] → 401 → ② 디바운스 재시작 1회(retry 1) → 두 번째 401 → 접음(retry 0 · 변경 시각 NULL). (AI badtoken)
  s5-finish    정상 AI 로 바꾼 뒤 30초 동안 아무것도 안 나가야 함(접은 변경은 다음 변경까지 유실) → [3,4] 저장 → 새 번호 → COMPLETED. (AI 정상)
  s6-observe   AI 프로세스는 살아 있고 **DB 만 꺼진** 채 사용자1 [3] → 40초 관찰: /health 가 store.connected=false 라 BE 가 틱을 건너뛰고 번호 유지. (AI 정상 · AI DB 컨테이너 중지)
  s6-recover   DB 켠 뒤 → 다음 틱에 새 번호 한 건 → DELIVERED.                                                   (AI 정상 · DB 복구)
  report       BE 프로파일 1·2 + AI 실행 기록을 JSON 으로.

왜 — 각 시험이 지키는 것:
  s1   연동의 바닥. 저장 후 디바운스(10s)+틱 안에 7.6 → 202 → 7.7 200 → COMPLETED 가 되는지. 이게 안 되면 나머지는 의미가 없다.
  s2   D1(10-06 실측 결함): AI 가 죽어 있으면 BE 가 틱마다 번호만 올리며 무한 재전송했다 — v0.7 ①③ 핑이 틱을 건너뛰고 변경 시각을 보존해
       복구 뒤 **한 번만** 나가는지.
  s34  D2(10-06 실측 결함): 7.7 이 못 닿으면 PENDING 에 영영 갇히고 이후 변경까지 막혔다 — v0.7 ⑧(PENDING 중 새 번호)·⑤(maximum-window 뒤
       같은 번호 복구 전송)으로 풀렸는지, AI 가 같은 번호를 재분석 없이 재전송(decide → RESEND)하는지.
       + A5(10-08 콜백 브랜치 09021a6): 더 새 번호가 전달되면 낮은 미전달 행을 SUPERSEDED 로 내려 /health undelivered 가 0 으로 돌아오는지.
  s5   v0.7 ②⑥: 7.6 이 재시도 불가 실패(401)를 받으면 디바운스 재시작은 **1회뿐**이고 두 번째면 접는지(옛 '같은 번호 2회 재시도'가 정말 사라졌는지),
       접은 변경은 다음 변경까지 유실된다는 대가가 설계대로인지.
  s6   v0.7 ① 핑 게이트의 둘째 조건: 프로세스는 살아 있어도 **DB 가 죽으면** /health 가 store.connected=false(+catalog.active=false)를 내고 BE 가
       틱을 건너뛰는지 — 10-08 낮에는 프로세스 꺼짐(s2)만 봤다. 이 게이트가 없으면 7.6 이 500/503 을 받아 BE 의 재시작 1회를 소모한다.

전제: BE .env 로 디바운스가 quiet 10s · maximum-window 60s · 틱 10s 로 줄어 있고, AI 는 CATALOG_SOURCE=db · 같은 서비스 토큰.
로그인 비밀번호는 BE 시드 V5 로컬 값(drive.py 기본). 운영 계정으로 돌리지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

# 같은 폴더의 drive.py — 로그인 · 비선호 저장 · SQL · AI 실행 기록 조회
from drive import Ai, Be, get_json, log, wait

USERS = ("minsoo.kim@gift.local", "jiyeon.lee@gift.local")           # BE 시드의 사용자 1·2
COLS = ("profile_status", "source_version", "analyzed_source_version", "retry_count", "last_changed_at", "window_started_at", "pending_since")


def be_profile(be: Be, uid: int) -> dict | None:
    rows = be.sql(f"select {', '.join(COLS)} from recipient_profiles where recipient_id={uid}")
    return dict(zip(COLS, rows[0])) if rows else None


def short(p: dict | None) -> str:
    if p is None:
        return "없음"
    return (f"{p['profile_status']} v{p['source_version']}/analyzed {p['analyzed_source_version']} retry {p['retry_count']} "
            f"changed={'-' if p['last_changed_at'] == 'NULL' else p['last_changed_at'][11:19]} "
            f"pending={'-' if p['pending_since'] == 'NULL' else p['pending_since'][11:19]}")


def ai_runs(ai: Ai, uid: int) -> list[dict]:
    return [{"v": r["source_version"], "status": r["status"], "attempts": r["callback_attempts"],
             "error": (r["error"] or {}).get("code") if r["error"] else None} for r in ai.runs(uid)]


def ai_run(ai: Ai, uid: int, v: int) -> dict | None:
    return next((r for r in ai_runs(ai, uid) if r["v"] == v), None)


def sample(be: Be, ai: Ai, uid: int, secs: float, step: float = 5.0) -> list[dict]:
    """secs 동안 step 마다 BE 프로파일 + AI 실행 기록을 찍는다 — 바뀐 순간이 보이게."""
    out, t0, last = [], time.time(), None
    while time.time() - t0 <= secs:
        try:
            ai_part: object = ai_runs(ai, uid)
        except Exception as e:  # noqa: BLE001 — s6: AI DB 가 꺼진 동안은 "못 읽음"으로 기록하고 계속 본다
            ai_part = f"AI DB 못 읽음({type(e).__name__})"
        cur = {"t": round(time.time() - t0), "be": be_profile(be, uid), "ai": ai_part}
        if (cur["be"], cur["ai"]) != last:
            log(f"  t+{cur['t']:>3}s BE {short(cur['be'])} · AI {cur['ai']}")
            out.append(cur)
            last = (cur["be"], cur["ai"])
        time.sleep(step)
    return out


def current_dislikes(be: Be, uid: int) -> list[int]:
    return sorted(int(r[0]) for r in be.sql(f"select category_id from user_dislike_categories where user_id={uid} and deleted_at is null"))


def changed_set(be: Be, uid: int, candidates: list[list[int]]) -> list[int]:
    """후보 중 지금 저장된 비선호와 **다른** 첫 집합. BE 비선호 저장은 멱등이라(PR #262) 같은 집합을 다시 저장하면 변경 기록이 없어 7.6 이 안 나간다
    — 시나리오를 이어 돌릴 때 앞 시나리오가 남긴 값과 겹치면 시험이 헛돈다(10-08 저녁 S6 뒤 S5 에서 실제로 겪음)."""
    cur = current_dislikes(be, uid)
    for c in candidates:
        if sorted(c) != cur:
            return c
    raise AssertionError(f"후보 {candidates} 가 전부 현재 값 {cur} 과 같다")


def save(out: Path | None, key: str, value) -> None:
    if out is None:
        return
    data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    data[key] = value
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------- 명령


def cmd_reset(be: Be, ai: Ai, a) -> dict:
    for uid in (1, 2):
        ai.cleanup(uid)
    be.sql("delete from recipient_recommended_products where recipient_id in (1,2); "
           "delete from recipient_profiles where recipient_id in (1,2); "
           "delete from user_dislike_categories where user_id in (1,2);")
    r = {"be": {u: be_profile(be, u) for u in (1, 2)}, "ai": {u: ai_runs(ai, u) for u in (1, 2)}}
    log(f"reset 완료 — BE {r['be']} · AI {r['ai']}")
    return r


def cmd_s1(be: Be, ai: Ai, a) -> dict:
    tok, u = be.login(USERS[0]), be.user_id(USERS[0])
    log(f"S1 사용자{u} 비선호 [1] 저장 → HTTP {be.put_dislikes(tok, [1])} · BE {short(be_profile(be, u))}")
    t0 = time.time()
    run, took = wait(lambda: ai_run(ai, u, 1) if (r := ai_run(ai, u, 1)) and r["status"] != "RUNNING" else None, a.wait_dispatch)
    prof, _ = wait(lambda: (p := be_profile(be, u)) and p["profile_status"] == "COMPLETED" and p["analyzed_source_version"] == "1" and p, 30)
    r = {"took_s": took, "ai": ai_runs(ai, u), "be": be_profile(be, u)}
    ok = bool(run) and run["status"] == "DELIVERED" and bool(prof)
    log(f"S1 {'✓' if ok else '✗'} {round(time.time() - t0, 1)}초 — AI {r['ai']} · BE {short(r['be'])}")
    return {"ok": ok, **r}


def cmd_s2_observe(be: Be, ai: Ai, a) -> dict:
    assert get_json(f"{a.ai}/health") is None, "AI 가 떠 있다 — s2-observe 는 AI 를 끈 채 돌린다"
    tok, u = be.login(USERS[0]), be.user_id(USERS[0])
    before = be_profile(be, u)
    ids = changed_set(be, u, [[1, 2], [1, 2, 3]])
    log(f"S2 AI 꺼짐 확인 · 사용자{u} 비선호 {ids} 저장 → HTTP {be.put_dislikes(tok, ids)} · 40초 관찰 (번호 {before['source_version']} 유지돼야)")
    samples = sample(be, ai, u, 40)
    after = be_profile(be, u)
    ok = after["source_version"] == before["source_version"] and after["last_changed_at"] != "NULL"
    log(f"S2-observe {'✓' if ok else '✗'} 번호 {before['source_version']} → {after['source_version']} · 변경 시각 {after['last_changed_at']}")
    return {"ok": ok, "before": before, "after": after, "samples": samples}


def cmd_s2_recover(be: Be, ai: Ai, a) -> dict:
    u = be.user_id(USERS[0])
    before = be_profile(be, u)
    # 기준 번호는 s2-observe 가 저장한 값에서 — AI 를 켜자마자 첫 틱에 새 번호가 나가므로 지금 읽으면 이미 올라가 있다(10-08 S2 실측 15:33:5x)
    base = before["source_version"]
    if a.out and a.out.exists():
        base = json.loads(a.out.read_text(encoding="utf-8")).get("s2-observe", {}).get("after", {}).get("source_version", base)
    nxt = int(base) + 1
    log(f"S2 AI 켜짐: {get_json(f'{a.ai}/health') is not None} · 번호 {nxt} 한 건이 나와 DELIVERED 돼야")
    run, took = wait(lambda: (r := ai_run(ai, u, nxt)) and r["status"] == "DELIVERED" and r, a.wait_dispatch)
    prof, _ = wait(lambda: (p := be_profile(be, u)) and p["analyzed_source_version"] == str(nxt) and p, 30)
    r = {"took_s": took, "ai": ai_runs(ai, u), "be": be_profile(be, u)}
    ok = bool(run) and bool(prof) and int(r["be"]["source_version"]) == nxt
    log(f"S2-recover {'✓' if ok else '✗'} {took}초 — AI {r['ai']} · BE {short(r['be'])}")
    return {"ok": ok, **r}


def cmd_s34_start(be: Be, ai: Ai, a) -> dict:
    tok, u = be.login(USERS[1]), be.user_id(USERS[1])
    log(f"S3 콜백 불통 AI 아래 사용자{u} 비선호 [2] 저장 → HTTP {be.put_dislikes(tok, [2])}")
    r1, took1 = wait(lambda: (r := ai_run(ai, u, 1)) and r["status"] == "RESULT_READY" and r["attempts"] >= 3 and r, a.wait_dispatch)
    p1 = be_profile(be, u)
    log(f"S3 {took1}초 — AI v1 {r1} · BE {short(p1)}  (PENDING 이어야)")
    log(f"S4 PENDING 중 재변경 [2,3] → HTTP {be.put_dislikes(tok, [2, 3])} · 새 번호 v2 가 나와야(⑧)")
    r2, took2 = wait(lambda: (r := ai_run(ai, u, 2)) and r["status"] == "RESULT_READY" and r, a.wait_dispatch)
    p2 = be_profile(be, u)
    log(f"S4 {took2}초 — AI v2 {r2} · BE {short(p2)}")
    log("S3 복구 전송 대기 — pending_since + 60s 뒤 같은 번호 v2 로 7.6 이 와야(⑤). AI 는 재전송(attempts 증가), BE 는 pending_since 재시작")
    samples = sample(be, ai, u, 100)
    p3, r3 = be_profile(be, u), ai_run(ai, u, 2)
    ok = (p1 is not None and p1["profile_status"] == "PENDING" and r1 is not None
          and r2 is not None and p2 is not None and p2["source_version"] == "2"
          and r3 is not None and r3["attempts"] > r2["attempts"] and p3["pending_since"] != p2["pending_since"])
    log(f"S3/S4-start {'✓' if ok else '✗'} — AI {ai_runs(ai, u)} · BE {short(p3)}")
    return {"ok": ok, "after_v1": {"ai": r1, "be": p1}, "after_v2": {"ai": r2, "be": p2}, "after_recovery": {"ai": r3, "be": p3}, "samples": samples}


def cmd_s34_finish(be: Be, ai: Ai, a) -> dict:
    u = be.user_id(USERS[1])
    log(f"S3 정상 AI 켜짐: {get_json(f'{a.ai}/health') is not None} · 다음 복구 전송(≤ 70s) → 재전송 → 7.7 200 → COMPLETED v2")
    samples = sample(be, ai, u, 100)
    p, r, r1 = be_profile(be, u), ai_run(ai, u, 2), ai_run(ai, u, 1)
    undelivered = ((get_json(f"{a.ai}/health") or {}).get("store") or {}).get("undelivered")
    # A5(09021a6): v2 가 DELIVERED 되는 순간 v1(미전달)은 BE 가 다시 묻지 않는 번호 → SUPERSEDED, /health undelivered 는 0 이어야 한다
    ok = (p is not None and p["profile_status"] == "COMPLETED" and p["analyzed_source_version"] == "2" and r is not None and r["status"] == "DELIVERED"
          and r1 is not None and r1["status"] == "SUPERSEDED" and r1["error"] == "CALLBACK_STALE" and undelivered == 0)
    log(f"S3/S4-finish {'✓' if ok else '✗'} — AI {ai_runs(ai, u)} · BE {short(p)} · A5: v1 {r1 and r1['status']} · undelivered {undelivered} (SUPERSEDED · 0 이어야)")
    return {"ok": ok, "ai": ai_runs(ai, u), "be": p, "undelivered": undelivered, "samples": samples}


def cmd_s5_start(be: Be, ai: Ai, a) -> dict:
    h = get_json(f"{a.ai}/health")
    assert h is not None, "AI(토큰 틀린 것)가 떠 있어야 한다"
    tok, u = be.login(USERS[0]), be.user_id(USERS[0])
    before = be_profile(be, u)
    ids = changed_set(be, u, [[3], [4]])
    log(f"S5 토큰 틀린 AI 아래 사용자{u} 비선호 {ids} 저장 → HTTP {be.put_dislikes(tok, ids)} · 50초 관찰 (401 → 재시작 1회 → 접음)")
    samples = sample(be, ai, u, 50, step=2)
    after = be_profile(be, u)
    versions = sorted({int(s["be"]["source_version"]) for s in samples})
    retries = [int(s["be"]["retry_count"]) for s in samples]
    ok = (after["retry_count"] == "0" and after["last_changed_at"] == "NULL" and after["window_started_at"] == "NULL"
          and 1 in retries and int(after["source_version"]) == int(before["source_version"]) + 2
          and not [r for r in ai_runs(ai, u) if r["v"] > int(before["source_version"])])
    log(f"S5-start {'✓' if ok else '✗'} 번호 {before['source_version']} → {after['source_version']} (본 번호 {versions}) · retry 흐름 {retries} · AI 새 기록 없음(401 은 접수 전)")
    return {"ok": ok, "before": before, "after": after, "samples": samples}


def cmd_s5_finish(be: Be, ai: Ai, a) -> dict:
    tok, u = be.login(USERS[0]), be.user_id(USERS[0])
    before = be_profile(be, u)
    log(f"S5 정상 AI 켜짐: {get_json(f'{a.ai}/health') is not None} · 30초 동안 아무것도 안 나가야(접은 변경은 유실)")
    quiet = sample(be, ai, u, 30)
    mid = be_profile(be, u)
    nothing = mid["source_version"] == before["source_version"]
    ids = changed_set(be, u, [[3, 4], [4, 5]])
    log(f"S5 30초 뒤 번호 {mid['source_version']} ({'✓ 그대로' if nothing else '✗ 올라감'}) · 이제 {ids} 저장 → HTTP {be.put_dislikes(tok, ids)}")
    nxt = int(mid["source_version"]) + 1
    run, took = wait(lambda: (r := ai_run(ai, u, nxt)) and r["status"] == "DELIVERED" and r, a.wait_dispatch)
    prof, _ = wait(lambda: (p := be_profile(be, u)) and p["analyzed_source_version"] == str(nxt) and p, 30)
    ok = nothing and bool(run) and bool(prof)
    log(f"S5-finish {'✓' if ok else '✗'} {took}초 — AI {ai_runs(ai, u)} · BE {short(be_profile(be, u))}")
    return {"ok": ok, "nothing_sent_30s": nothing, "quiet_samples": quiet, "ai": ai_runs(ai, u), "be": be_profile(be, u)}


def cmd_s6_observe(be: Be, ai: Ai, a) -> dict:
    h = get_json(f"{a.ai}/health")
    assert h is not None, "AI 프로세스는 떠 있어야 한다(DB 만 끈다)"
    assert (h.get("store") or {}).get("connected") is False, f"AI DB 가 꺼진 상태여야 한다 — health={h}"
    tok, u = be.login(USERS[0]), be.user_id(USERS[0])
    before = be_profile(be, u)
    log(f"S6 AI 살아 있음 · DB 꺼짐 (health 200: catalog.active={h['catalog'].get('active')} store.connected={h['store'].get('connected')}) · "
        f"사용자{u} 비선호 {(ids := changed_set(be, u, [[3], [4]]))} 저장 → HTTP {be.put_dislikes(tok, ids)} · 40초 관찰 (번호 {before['source_version']} 유지돼야)")
    samples = sample(be, ai, u, 40)
    after = be_profile(be, u)
    ok = after["source_version"] == before["source_version"] and after["last_changed_at"] != "NULL"
    log(f"S6-observe {'✓' if ok else '✗'} 번호 {before['source_version']} → {after['source_version']} · 변경 시각 {after['last_changed_at']}")
    return {"ok": ok, "health": h, "before": before, "after": after, "samples": samples}


def cmd_s6_recover(be: Be, ai: Ai, a) -> dict:
    u = be.user_id(USERS[0])
    base = be_profile(be, u)["source_version"]
    if a.out and a.out.exists():
        base = json.loads(a.out.read_text(encoding="utf-8")).get("s6-observe", {}).get("after", {}).get("source_version", base)
    nxt = int(base) + 1
    h, took_h = wait(lambda: (x := get_json(f"{a.ai}/health")) and (x.get("store") or {}).get("connected") and x, 60)
    log(f"S6 DB 복구 뒤 /health store.connected=true 까지 {took_h}초 · 번호 {nxt} 한 건이 나와 DELIVERED 돼야")
    run, took = wait(lambda: (r := ai_run(ai, u, nxt)) and r["status"] == "DELIVERED" and r, a.wait_dispatch)
    prof, _ = wait(lambda: (p := be_profile(be, u)) and p["analyzed_source_version"] == str(nxt) and p, 30)
    r = {"health_took_s": took_h, "took_s": took, "ai": ai_runs(ai, u), "be": be_profile(be, u)}
    ok = bool(h) and bool(run) and bool(prof) and int(r["be"]["source_version"]) == nxt
    log(f"S6-recover {'✓' if ok else '✗'} {took}초 — AI {r['ai']} · BE {short(r['be'])}")
    return {"ok": ok, **r}


def cmd_report(be: Be, ai: Ai, a) -> dict:
    r = {"be": {u: be_profile(be, u) for u in (1, 2)}, "ai": {u: ai_runs(ai, u) for u in (1, 2)},
         "ai_health": get_json(f"{a.ai}/health")}
    log(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return r


COMMANDS = {"reset": cmd_reset, "s1": cmd_s1, "s2-observe": cmd_s2_observe, "s2-recover": cmd_s2_recover,
            "s34-start": cmd_s34_start, "s34-finish": cmd_s34_finish, "s5-start": cmd_s5_start, "s5-finish": cmd_s5_finish,
            "s6-observe": cmd_s6_observe, "s6-recover": cmd_s6_recover,
            "report": cmd_report}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--be", default="http://127.0.0.1:8080")
    ap.add_argument("--ai", default="http://127.0.0.1:8000")
    ap.add_argument("--mysql-container", default=os.environ.get("BE_MYSQL_CONTAINER", "seonjalal-mysql-local"))
    ap.add_argument("--mysql-user", default=os.environ.get("BE_MYSQL_USER", "gift"))
    ap.add_argument("--mysql-password", default=os.environ.get("BE_MYSQL_PASSWORD", "gift_local_pw"))
    ap.add_argument("--seed-password", default=os.environ.get("BE_SEED_PASSWORD", "Test1234!"))
    ap.add_argument("--wait-dispatch", type=float, default=60, help="7.6 이 와서 끝나기까지 기다리는 최대 초 (디바운스 10s + 틱 10s)")
    ap.add_argument("--out", type=Path, default=None, help="결과 JSON (명령별 키로 덧붙임)")
    a = ap.parse_args()
    be = Be(a.be, a.mysql_container, a.mysql_user, a.mysql_password, a.seed_password)
    ai = Ai(a.ai)
    result = COMMANDS[a.command](be, ai, a)
    save(a.out, a.command, result)
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
