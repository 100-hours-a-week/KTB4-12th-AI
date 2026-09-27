"""BE(develop) ↔ AI 연동 자체 시험 드라이버 — 로그인 → 비선호 저장 → BE 스케줄러가 7.6 → AI 분석 → 7.7 까지를 관찰한다.

전제 (docs/BE_연동_시험_결과_2026-09-27.md §7 재현 방법):
  - BE 가 :8080 (로컬 .env, 디바운스를 초 단위로 줄여서) · AI 가 :8000 (같은 서비스 토큰, CATALOG_SOURCE=db) 떠 있다
  - BE MySQL 은 docker 컨테이너(기본 seonjalal-mysql-local) — 증거를 SQL 로 읽으려고 docker exec 를 쓴다
  - **profiling/ 디렉터리에서** `uv run python tools/be_integration/drive.py` 로 실행 (AI 설정·DB 를 profiling 의 것으로 읽는다)

시험:
  T1  사용자 A 비선호 [대분류 1 뷰티] → 7.6 → 202 → AI 30개(뷰티 0개여야) → 7.7 결과 (BE PR3 전이면 401 → AI FAILED/CONTRACT_7_7_REJECTED)
  T3  A 가 PENDING 인 동안 재변경 → BE develop 은 새 7.6 을 보내지 않는다(PENDING 제외 · 타임아웃 없음 — PR 5-2 전)
  T2  (--with-ai-down) AI 를 내리고 사용자 B 비선호 → BE 가 틱마다 새 번호로 재시도하는지 관찰 → AI 를 다시 올려 복구 확인.
      **이 단계는 `pkill -f "uvicorn profiling.main:app"` 으로 AI 를 죽이고 스크립트가 다시 띄운다.**
비밀번호는 BE 시드 V5 의 로컬 목 데이터 값(Test1234!)이 기본이고 BE_SEED_PASSWORD 로 바꾼다. 운영 계정으로 돌리지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import sqlalchemy as sa

from profiling.settings import get_settings

USERS = ("minsoo.kim@gift.local", "jiyeon.lee@gift.local")           # BE 시드 V2 의 사용자 1·2


def log(msg: str) -> None:
    print(f"[{datetime.now(UTC).astimezone().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_json(url: str) -> dict | None:
    try:
        r = httpx.get(url, timeout=2)
        return r.json() if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


def wait(cond, secs: float, step: float = 1.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        v = cond()
        if v:
            return v, round(time.time() - t0, 1)
        time.sleep(step)
    return None, secs


class Be:
    def __init__(self, base: str, container: str, user: str, password: str, seed_password: str) -> None:
        self.base, self.container, self.user, self.password, self.seed_password = base, container, user, password, seed_password

    def sql(self, q: str) -> list[list[str]]:
        r = subprocess.run(["docker", "exec", self.container, "mysql", "-N", f"-u{self.user}", f"-p{self.password}", "gift", "-e", q],
                           capture_output=True, text=True, check=False)
        return [line.split("\t") for line in r.stdout.strip().splitlines()]

    def profile(self, uid: int) -> list[str] | None:
        rows = self.sql("select profile_status, source_version, analyzed_source_version, retry_count, last_changed_at, pending_since "
                        f"from recipient_profiles where recipient_id={uid}")
        return rows[0] if rows else None

    def user_id(self, email: str) -> int:
        return int(self.sql(f"select id from users where email='{email}'")[0][0])

    def parents_of(self, ids: list[int]) -> dict[int, int]:
        if not ids:
            return {}
        rows = self.sql("select c.parent_id, count(*) from products p join categories c on c.id=p.category_id "
                        f"where p.id in ({','.join(map(str, ids))}) group by c.parent_id order by 1")
        return {int(a): int(b) for a, b in rows}

    def login(self, email: str) -> str:
        r = httpx.post(f"{self.base}/auth/login", json={"email": email, "password": self.seed_password},
                       headers={"Origin": "http://localhost:3000"}, timeout=10)
        assert r.status_code == 200, (r.status_code, r.text[:300])
        return r.json()["data"]["accessToken"]

    def put_dislikes(self, token: str, ids: list[int]) -> int:
        r = httpx.put(f"{self.base}/preferences/dislike-categories", json={"categoryIds": ids},
                      headers={"Authorization": f"Bearer {token}", "Origin": "http://localhost:3000"}, timeout=10)
        return r.status_code


class Ai:
    def __init__(self, base: str) -> None:
        self.base = base
        self.engine = sa.create_engine(get_settings().DATABASE_URL, future=True)

    def runs(self, uid: int) -> list:
        with self.engine.connect() as c:
            return c.execute(sa.text("select source_version, status, error, callback_attempts, callback_payload, catalog_version_id "
                                     "from ai_profile.profile_runs where recipient_user_id=:r order by source_version"), {"r": uid}).mappings().all()

    def finished(self, uid: int):
        return [r for r in self.runs(uid) if r["status"] != "RUNNING"] or None

    def cleanup(self, uid: int) -> None:
        from profiling.stores import DbProfileRunStore, DbRecipientProfileStore
        DbProfileRunStore(self.engine).delete_recipient(uid)
        DbRecipientProfileStore(self.engine).delete(uid)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--be", default="http://127.0.0.1:8080")
    ap.add_argument("--ai", default="http://127.0.0.1:8000")
    ap.add_argument("--mysql-container", default=os.environ.get("BE_MYSQL_CONTAINER", "seonjalal-mysql-local"))
    ap.add_argument("--mysql-user", default=os.environ.get("BE_MYSQL_USER", "gift"))
    ap.add_argument("--mysql-password", default=os.environ.get("BE_MYSQL_PASSWORD", "gift_local_pw"))
    ap.add_argument("--seed-password", default=os.environ.get("BE_SEED_PASSWORD", "Test1234!"))
    ap.add_argument("--wait-dispatch", type=float, default=90, help="7.6 이 오기까지 기다리는 최대 초 (BE 디바운스+주기)")
    ap.add_argument("--with-ai-down", action="store_true", help="T2: AI 를 죽였다 살리며 BE 재시도 동작 관찰")
    ap.add_argument("--keep", action="store_true", help="끝나고 AI DB 의 시험 행을 지우지 않는다")
    ap.add_argument("--out", type=Path, default=None, help="결과 JSON 저장 경로")
    a = ap.parse_args()
    be, ai, R = Be(a.be, a.mysql_container, a.mysql_user, a.mysql_password, a.seed_password), Ai(a.ai), {}

    ok_be, _ = wait(lambda: (get_json(f"{a.be}/actuator/health") or {}).get("status") == "UP", 30, 2)
    ok_ai, _ = wait(lambda: (get_json(f"{a.ai}/health") or {}).get("status") == "ok", 30, 1)
    log(f"준비: BE UP={bool(ok_be)} · AI ok={bool(ok_ai)}")
    if not (ok_be and ok_ai):
        return 2
    R["ai_health"] = get_json(f"{a.ai}/health")
    ua, ub = be.user_id(USERS[0]), be.user_id(USERS[1])

    # T1
    tok = be.login(USERS[0])
    log(f"T1 사용자 {ua} 비선호 [1 뷰티] 저장 → HTTP {be.put_dislikes(tok, [1])}")
    run, took = wait(lambda: ai.finished(ua), a.wait_dispatch, 2)
    if not run:
        log(f"T1 실패: {a.wait_dispatch}초 안에 AI 실행 기록이 없다 — BE 스케줄러·디바운스·토큰을 확인")
        return 1
    r0 = run[0]
    ids = r0["callback_payload"]["recommendedProductIds"] if r0["callback_payload"] else []
    par = be.parents_of(ids)
    log(f"T1 {took}초 뒤 AI 실행 기록: v{r0['source_version']} {r0['status']} error={r0['error']} 7.7 시도 {r0['callback_attempts']}")
    log(f"T1 추천 {len(ids)}개의 대분류 분포 {par} → 뷰티(1) {par.get(1, 0)}개 {'✓' if par.get(1, 0) == 0 else '✗ 대분류 제외 실패'}")
    log(f"T1 BE profile: {be.profile(ua)}")
    R["T1"] = {"took_s": took, "run": {k: str(v) for k, v in r0.items() if k != "callback_payload"}, "ids": ids, "parents": par, "be": be.profile(ua)}

    # T3
    log(f"T3 PENDING 중 재변경 [1,2] → HTTP {be.put_dislikes(tok, [1, 2])} · 35초 관찰")
    time.sleep(35)
    R["T3"] = {"be": be.profile(ua), "ai_runs": len(ai.runs(ua))}
    log(f"T3 BE profile: {R['T3']['be']} · AI 실행 기록 수 {R['T3']['ai_runs']} (1 이면 새 7.6 없음)")

    # T2
    if a.with_ai_down:
        subprocess.run(["pkill", "-f", "uvicorn profiling.main:app"], check=False)
        time.sleep(3)
        log(f"T2 AI 중지: {get_json(f'{a.ai}/health') is None}")
        tokb = be.login(USERS[1])
        log(f"T2 사용자 {ub} 비선호 [2 패션] 저장 → HTTP {be.put_dislikes(tokb, [2])}")
        samples = []
        for i in range(4):
            time.sleep(10)
            p = be.profile(ub)
            samples.append(((i + 1) * 10, p[1] if p else None, p[0] if p else None))
        log(f"T2 AI 다운 40초: (초, source_version, status) {samples}")
        env = dict(os.environ, PROFILING_BACKEND_BASE_URL=a.be, PROFILING_CATALOG_SOURCE="db")
        subprocess.Popen(["uv", "run", "uvicorn", "profiling.main:app", "--port", a.ai.rsplit(":", 1)[1]], env=env, start_new_session=True)
        ok, _ = wait(lambda: (get_json(f"{a.ai}/health") or {}).get("status") == "ok", 60, 1)
        run2, took2 = wait(lambda: ai.finished(ub), 60, 2)
        R["T2"] = {"samples": samples, "ai_up": bool(ok), "run": {k: str(v) for k, v in run2[0].items() if k != "callback_payload"} if run2 else None, "be": be.profile(ub)}
        log(f"T2 AI 복구 뒤 {took2}초: " + (f"v{run2[0]['source_version']} {run2[0]['status']}" if run2 else "실행 기록 없음") + f" · BE profile {R['T2']['be']}")

    if a.out:
        a.out.write_text(json.dumps(R, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        log(f"결과 JSON → {a.out}")
    if not a.keep:
        for uid in (ua, ub):
            ai.cleanup(uid)
        log("AI DB 의 시험 행 정리 (BE MySQL 은 그대로 — 보고서 §7 의 초기화 SQL 참고)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
