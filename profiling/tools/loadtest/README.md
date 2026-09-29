# 부하 시험 하네스 — `tools/loadtest/`

7.6 을 **동시에 N건** 보내고, 그동안 `/health` 지연 · DB 커넥션 · 완료 시각을 잰다. BE 실물이 보내는 동안 AI 쪽만 재는 모드도 있다. 결과 기록은 [부하_시험_결과_2026-09-29.md](../../docs/부하_시험_결과_2026-09-29.md)(수정 전후, 팀 공유용) · [2026-09-28.md](../../docs/부하_시험_결과_2026-09-28.md)(기준선).

새 의존성 없음(httpx · SQLAlchemy). 서버는 **사용자 동의 뒤에** 띄운다. v1 은 모델을 쓰지 않아 유료 호출이 없다.

## 파일

| 파일 | 무엇 |
|---|---|
| `run.py` | 하네스. `uv run python -m tools.loadtest.run …` (profiling/ 에서, `-m` 필수 — `tools/` 가 네임스페이스 패키지) |
| `be_seed.sql` · `be_reset.sql` · `be_cleanup.sql` | BE 실물 구동(S-C)용 — 로컬 MySQL 에 시험 사용자 900001~ 를 넣고·되돌리고·지운다 |
| `results/` | JSON·로그 저장 자리(커밋 안 함) |

## 전제

```bash
cd profiling && docker compose up -d && uv run alembic upgrade head      # 카탈로그가 적재돼 있어야 (README §3)
uv run uvicorn tools.fake_backend.app:app --port 8081                    # T1: 7.7 싱크 (모르는 수신자도 200)
PROFILING_CATALOG_SOURCE=db PROFILING_BACKEND_BASE_URL=http://localhost:8081 PROFILING_SLOTS=1 \
  uv run uvicorn profiling.main:app --port 8000 --timeout-graceful-shutdown 5 2>&1 | tee tools/loadtest/results/ai-S-A1.log   # T2 (--reload 없이)
```

`curl :8000/health` 에서 `catalog.products: 4231` · `supervisor.slots` 가 띄운 값인지 본다(`--expect-slots` 가 같은 검사를 한다).

## 돌리기

```bash
uv run python -m tools.loadtest.run --n 100 --concurrency 100 --base 900001 --cleanup --yes --expect-slots 1 \
    --label S-A1 --out tools/loadtest/results/S-A1.json
```

| 옵션 | 뜻 |
|---|---|
| `--mode burst\|be` | burst: 하네스가 7.6 을 보낸다 · be: 보내지 않고 BE 가 보내는 동안 잰다(`--ai-log` 로 도착 시각) |
| `--n` `--concurrency` | 건수 · 동시성(수신자 `--base`~`base+n-1`, 각 `--source-version` 하나) |
| `--dislike rotate\|1,2` | 비선호 대분류 — 수신자마다 1~10 순환, 또는 고정 목록(최대 5 — 7.6 상한, 넘기면 400) |
| `--fake-mode ok\|500\|timeout\|409\|400` | 시작 전 가짜 백엔드 실패 주입, 끝나면 ok 로 복구. `500` 은 슬롯당 ≈2.5초, `timeout` 은 ≈17.5초 |
| `--health-interval 0.25` `--health-threshold-ms 1000` `--probe-timeout 10` | `/health` 순차 프로브 |
| `--db-interval 0.5` | `pg_stat_activity` 커넥션 · 구간 진행 표본 |
| `--post-timeout 30` `--wait 120` `--tail 5` | 7.6 응답 대기 · 전부 종료 대기 · 종료 뒤 추가 관찰 |
| `--cleanup --yes` | 시작 전·끝난 뒤 구간의 `profile_runs`·`recipient_profiles` 삭제. `--base` 900000 이상만 |
| `--cleanup-only` | 정리만(RUNNING 이 남았으면 거부 → AI 를 내린 뒤) |
| `--expect-slots k` | `/health supervisor.slots` 가 k 가 아니면 중단 |
| `--out` `--label` `--no-samples` | JSON 저장 · 이름표 · 표본 배열 제외 |
| `--no-keepalive` | 7.6 마다 새 TCP 연결(`Connection: close`). **동시성 100 이상에서 서버 접수 능력을 잴 때 켠다** — 아래 주의 |

종료 코드: 0 전부 종료 · 1 시간 초과 · 2 사전 점검·가드 실패.

## 지표

| 지표 | 출처 | 뜻 |
|---|---|---|
| `post.ms` p50/p95/max · `over_10s` | 하네스 시계 | 202 응답 지연. 10초 초과 = BE(read timeout 10s)라면 실패로 보고 새 번호로 재전송할 건수 |
| `health.ms` · `over_threshold` · `failed` | 하네스 시계 | `/health` 가 멈추는지. 프로브는 순차라 멈춘 시간이 그대로 표본 |
| `runs.slot_ms` | DB `updated_at − created_at` | 슬롯을 쥔 시간(분석 + 콜백). 대기는 포함되지 않음 |
| `runs.e2e_ms` | 보낸 시각(하네스) → `updated_at`(DB, t0 에 잰 `now()` 로 정렬) | 접수→종료. be 모드는 로그 도착 시각 기준 |
| `runs.time_to_all_terminal_s` · `throughput_per_s` | DB | 전부 끝나기까지 · 처리량 |
| `db.connections_max` · `idle_in_tx_max` | `pg_stat_activity`(하네스 제외) | 커넥션 압박 · 열린 트랜잭션 |
| `db.waiting_max` | 접수됐으나 미종료·미실행(근사) | 접수 대기 건수 근사 — Supervisor 수정 전에는 40 을 넘으면 스레드풀 토큰이 바닥났다 |
| `health.queued_max` · `rejected_delta` | `/health supervisor.queued`·`rejected` | 실측 큐 깊이(09-28 이후)와 503 으로 거절된 수 |
| `runs.multi_row_recipients` · `callback_attempts_total` · `received_delta` | DB · 가짜 `/received` | 재전송 흔적 · 콜백 시도 · 실제 도착 |

## BE 실물 구동 (S-C)

```bash
docker exec -i seonjalal-mysql-local mysql -N -ugift -p<로컬값> gift < tools/loadtest/be_seed.sql       # 200명
# AI 는 7.7 을 가짜 싱크로 보내도록 띄운다 (BE 에는 아직 7.7 수신이 없다)
PROFILING_SERVICE_TOKEN=local-profiling-token PROFILING_BACKEND_BASE_URL=http://localhost:8081 PROFILING_CATALOG_SOURCE=db PROFILING_SLOTS=1 \
  uv run uvicorn profiling.main:app --port 8000 2>&1 | tee tools/loadtest/results/ai-S-C.log
uv run python -m tools.loadtest.run --mode be --n 200 --base 900001 --wait 180 --cleanup --yes \
    --ai-log tools/loadtest/results/ai-S-C.log --label S-C --out tools/loadtest/results/S-C.json
# BE (KTB4-12th-BE, 무수정): set -a && . ./.env && set +a && AI_PROFILE_DISPATCH_INTERVAL=10s AI_PROFILE_BATCH_SIZE=100 \
#   APP_AIPROFILE_QUIETPERIOD=10s APP_AIPROFILE_MAXIMUMWINDOW=60s SCHEDULING_ENABLED=true JAVA_HOME=… ./gradlew bootRun
```

재실행은 `be_reset.sql`(버전은 유지 — 0 으로 되돌리면 AI 가 재전송으로 판정) + 하네스 `--cleanup`. 끝에 `be_cleanup.sql`(남은 수 0).

## 주의

- 로컬 Mac 수치는 상대 기준선이다. 하네스·uvicorn·Docker DB 가 같은 코어를 나눠 쓴다.
- 시나리오 사이마다 `--cleanup`. `/health` 의 `undelivered` 는 `count(*)` 순차 스캔이라 행 수에 비례해 느려진다.
- 클라이언트 타임아웃은 서버 작업을 취소하지 않는다. 완료는 DB 행으로 센다. RUNNING 이 남으면 정리를 거부한다.
- `--fake-mode timeout` 은 슬롯 1 에서 100건에 약 29분 — `--wait 120` 부분 실행 뒤 AI 를 내리고 `--cleanup-only`.
- **하네스 자체의 한계(09-29 확인)**: httpx 풀이 keep-alive 연결을 100개쯤 쥐면 요청 배정이 느려져 초당 50~80건이 상한이 된다(httpcore 1.0.9). 같은 동시성으로 `/openapi.json` 을 때려도 같아서 서버 문제가 아니다 — 원시 소켓 keep-alive 클라이언트는 8,000/s, `Connection: close` 는 900/s 가 나온다. `--n` 이 `--concurrency` 보다 크면 `--no-keepalive` 로 재고, keep-alive 값은 '클라이언트 포함 지연'으로만 읽는다. 09-28 S-A′(300건) 의 202 p50 1.0초·p95 3.7초는 이 한계가 섞인 값이다([부하_시험_결과_2026-09-29 §4.3](../../docs/부하_시험_결과_2026-09-29.md)).
