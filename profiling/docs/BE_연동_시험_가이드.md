# BE ↔ AI 연동 로컬 시험 가이드 (2026-10-06)

BE(`KTB4-12th-BE`, develop)와 AI 프로파일링 서버를 내 Mac에서 같이 띄우고, 7.6(요청) → 분석 → 7.7(결과)이 실제로 오가는지 혼자 확인하는 절차다. 2026-10-06에 이 절차 그대로 돌려 전부 통과했고(§9), 그때 쓴 명령을 그대로 적었다. 결과 기록은 [BE_연동_시험_결과_2026-09-27.md](BE_연동_시험_결과_2026-09-27.md)(첫 시험)와 변경 안내(`KTB4_12team/AI/제안서/BE_연동_필드표/`)에 있다.

| 절 | 내용 |
|---|---|
| 1 | 전제 — 설치돼 있어야 하는 것 |
| 2 | 켜기 — DB → AI → BE 순서 (터미널 3개) |
| 3 | 초기화 — 다시 돌리기 전에 지우는 것 |
| 4 | 시험 A — 자동 드라이버 한 번에 |
| 5 | 시험 B — 수동으로 한 바퀴 (로그인 → 비선호 저장 → 관찰) |
| 6 | 시험 C — BE 콜백 API에 오류 응답 직접 받아 보기 |
| 7 | 시험 D — 장애 재현 (AI 꺼짐 · 7.7 불통) |
| 8 | 확인 SQL·로그 모음 · 끄기 · 자주 걸리는 것 |
| 9 | 10-06에 본 "정상" 값 (비교 기준) |

---

## 1. 전제

| 필요한 것 | 확인 |
|---|---|
| Docker Desktop 실행 중 | `docker info`가 오류 없이 나온다 |
| `uv` | `which uv` |
| JDK 25 (Homebrew `openjdk@25`) | `ls /opt/homebrew/opt/openjdk@25/libexec/openjdk.jdk/Contents/Home/bin/java` |
| 두 저장소가 나란히 | `/Users/emet/KTB4-12th/KTB4-12th-AI` · `/Users/emet/KTB4-12th/KTB4-12th-BE` |
| DB 컨테이너 2개가 있다 | `docker ps -a`에 `seonjalal-mysql-local`(MySQL 8, BE)와 `profiling-ai-db`(pgvector pg16, AI) |
| BE `.env` | BE 저장소 루트. **내용을 화면에 찍지 않는다**(비밀값). 필요한 키: `AI_PROFILE_BASE_URL=http://localhost:8000` · `PROFILING_SERVICE_TOKEN`(AI와 같은 값, 로컬은 `local-profiling-token`) · 로컬 단축값 `AI_PROFILE_DISPATCH_INTERVAL=10s` · `APP_AIPROFILE_QUIETPERIOD=10s` · `APP_AIPROFILE_MAXIMUMWINDOW=60s`. 키가 있는지만 보려면 `grep -c '^PROFILING_SERVICE_TOKEN=' .env` |

컨테이너가 없으면 만든다 — MySQL은 [09-27 보고서 §7](BE_연동_시험_결과_2026-09-27.md) 1번의 `docker run …`, AI DB는 `profiling/`에서 `docker compose up -d ai-db`(**`ai-db`만**. 그냥 `up -d`를 하면 배포용 `ai-app` 컨테이너까지 8000번에 뜬다).

## 2. 켜기 — DB → AI → BE 순서

**순서가 중요하다.** BE를 먼저 켜고 AI가 없으면, 대기 중인 변경이 있을 때 BE가 10초마다 7.6을 실패하며 번호를 올린다(§7 D1). AI를 먼저 올려 두면 깨끗하다.

### 2-1. DB

```bash
docker start seonjalal-mysql-local profiling-ai-db
```

준비 확인(둘 다 오류 없이 끝나야 한다):

```bash
docker exec seonjalal-mysql-local sh -c 'mysqladmin ping -uroot -p"$MYSQL_ROOT_PASSWORD" --silent' && docker exec profiling-ai-db pg_isready -U ai_user -d ai_chat
```

### 2-2. AI (터미널 A)

```bash
cd /Users/emet/KTB4-12th/KTB4-12th-AI/profiling && uv run alembic upgrade head
```

```bash
cd /Users/emet/KTB4-12th/KTB4-12th-AI/profiling && PROFILING_SERVICE_TOKEN=local-profiling-token PROFILING_BACKEND_BASE_URL=http://localhost:8080 PROFILING_CATALOG_SOURCE=db uv run uvicorn profiling.main:app --port 8000
```

`profiling/.env`는 건드리지 않고 환경변수로 덮는다. 확인:

```bash
curl -s localhost:8000/health
```

정상이면 `"status":"ok"`, `"catalog":{"active":true,…,"products":4231}`, `"store":{…,"connected":true,…,"undelivered":0}`, `"supervisor":{"slots":1,…,"queue_max":200,…}`. `catalog.active`가 false면 카탈로그가 없는 것이다 — 7.6이 전부 503이 되므로 먼저 적재한다(`tools/catalog/returned/2026-09-25/README.md` 「다시 만들기」의 `load_catalog` 명령에 `--package ~/Downloads/product-catalog-20260922-v1`을 붙여서).

### 2-3. BE (터미널 B)

```bash
cd /Users/emet/KTB4-12th/KTB4-12th-BE && set -a && . ./.env && set +a && export AI_PROFILE_SCHEDULING_ENABLED=true SCHEDULING_ENABLED=true JAVA_HOME=/opt/homebrew/opt/openjdk@25/libexec/openjdk.jdk/Contents/Home && ./gradlew bootRun --console=plain
```

- `AI_PROFILE_SCHEDULING_ENABLED=true`가 **꼭** 있어야 한다. 기본값이 꺼짐이고 `.env`에도 이 키가 없어서, 빼먹으면 BE가 7.6을 영영 보내지 않는다(오류도 안 난다).
- 첫 기동은 Gradle 컴파일 때문에 1~2분. 로그에 `Started GiftApplication in …`이 나오면 끝. Flyway가 새 마이그레이션을 자동 적용한다(10-06에는 V12~V15가 적용됐다).
- 스케줄러가 도는지는 로그로 안다 — 10초마다 `select rp1_0.id from recipient_profiles …` 쿼리가 찍힌다.

## 3. 초기화 — 다시 돌리기 전에

시드 사용자 1(`minsoo.kim@gift.local`)·2(`jiyeon.lee@gift.local`)의 흔적을 지운다. **PENDING으로 남은 행이 있으면 그 사용자는 새 7.6이 안 나가므로**(BE는 PENDING 중 새 요청을 안 만든다) 지우지 않으면 시험이 안 된다.

```bash
docker exec seonjalal-mysql-local sh -c 'mysql -ugift -p"$MYSQL_PASSWORD" -D gift -e "delete from recipient_recommended_products where recipient_id in (1,2); delete from recipient_profiles where recipient_id in (1,2); delete from user_dislike_categories where user_id in (1,2);"'
```

AI 쪽 실행 기록(없어도 되지만 보기 편하게):

```bash
docker exec profiling-ai-db psql -U ai_user -d ai_chat -c "delete from ai_profile.profile_runs where recipient_user_id in (1,2);"
```

초기화 없이 새로 돌리려면 다른 시드 사용자를 쓴다(§5 표).

## 4. 시험 A — 자동 드라이버

`profiling/` 디렉터리에서(AI 설정·DB를 그 디렉터리 기준으로 읽는다):

```bash
cd /Users/emet/KTB4-12th/KTB4-12th-AI/profiling && uv run python tools/be_integration/drive.py --keep --out /tmp/drive_result.json
```

| 단계 | 하는 일 | 10-06 결과 |
|---|---|---|
| T1 | 사용자 1 로그인 → 비선호 [1 뷰티] 저장 → BE가 7.6 → AI 분석 → 7.7 | 12초 뒤 `v1 DELIVERED`, 추천 30개 중 뷰티 0개, BE `COMPLETED 1/1` |
| T3 | 곧바로 [1,2]로 재변경 → 35초 관찰 | BE `COMPLETED 2/2`, AI 실행 기록 2개 |

- `--keep`: 끝나고 AI 실행 기록을 지우지 않는다(§8 SQL로 보려면 필요).
- `--with-ai-down`(T2): 드라이버가 `pkill -f "uvicorn profiling.main:app"`으로 AI를 죽였다가 `uv run uvicorn …`으로 다시 띄운다. 다시 띄울 때 토큰은 **드라이버를 돌린 셸의 환경변수**를 물려받으므로, 쓰려면 먼저 `export PROFILING_SERVICE_TOKEN=local-profiling-token`을 해 둔다. 안 그러면 되살아난 AI가 다른 토큰으로 떠서 7.6이 401이 된다.

## 5. 시험 B — 수동으로 한 바퀴

시드 사용자(비밀번호는 시드 공통값, `drive.py`의 `--seed-password` 기본값과 같다):

| 번호 | 이메일 | 비고 |
|---|---|---|
| 1 | `minsoo.kim@gift.local` | 드라이버 T1·T3 |
| 2 | `jiyeon.lee@gift.local` | 드라이버 T2 |
| 3~ | `dohyun.jung` · `haneul.oh` · `jaehoon.lim` · `jimin.han` · `junhyuk.park` · `junyoung.seo` · `seoyeon.choi` · `seungho.yoon` · `somin.jang` · `yeeun.kang` (`@gift.local`) | 초기화 없이 새 시험에 쓰기 좋다 |

1) 로그인 → `data.accessToken`을 복사한다.

```bash
curl -s -X POST localhost:8080/auth/login -H 'Content-Type: application/json' -d '{"email":"minsoo.kim@gift.local","password":"Test1234!"}'
```

2) 비선호 저장(대분류 id 1~10). `TOKEN`에 위 값을 넣는다.

```bash
curl -s -w '\nHTTP %{http_code}\n' -X PUT localhost:8080/preferences/dislike-categories -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"categoryIds":[1]}'
```

3) 10~20초 안에 일어나는 일(로컬 단축값 기준: 디바운스 10초 + 틱 10초):

| 어디 | 보이는 것 |
|---|---|
| AI 로그 | `7.6 접수 recipient=1 source_version=1 …` → `7.6 중복 판정 … → analyze (첫 접수)` → `profile … → RESULT_READY pool=30/30` → `7.7 콜백 recipient=1 source_version=1 → 200 DELIVERED ids=30` → `7.7 콜백 결과 … 시도 1/3 → DELIVERED` |
| BE 로그 | `HTTP request completed. method=POST, path=/api/internal/v1/recipients/{recipientUserId}/profile, status=200` |
| BE DB | `profile_status=COMPLETED`, `source_version=1`, `analyzed_source_version=1`, `pending_since=NULL` (§8 SQL) |
| AI DB | `profile_runs`에 `status=DELIVERED, callback_attempts=1` |

추천 30개에 비선호 대분류가 없는지 보려면 §8의 "대분류 분포" SQL.

## 6. 시험 C — BE 콜백 API에 오류 응답 직접 받아 보기

AI 흉내를 내서 BE의 7.7 수신 API를 직접 부른다. 사용자 1이 `COMPLETED 2/2`인 상태(시험 A 직후)를 기준으로 한 기대값이다.

```bash
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8080/api/internal/v1/recipients/1/profile -H 'Authorization: Bearer local-profiling-token' -H 'Content-Type: application/json' -d '{"recipientUserId":1,"sourceVersion":1,"profileStatus":"COMPLETED","recommendedProductIds":[1,2,3]}'
```

본문·헤더만 바꿔 가며 확인한 것:

| 보낸 것 | 응답 (10-06 실측) |
|---|---|
| 토큰 틀림 (`Bearer wrong`) | 401 `UNAUTHORIZED` |
| 저장본보다 오래된 번호 (`sourceVersion: 1`) | 409 `STALE_SOURCE_VERSION` |
| 번호 0 | **409** (PR 설명은 400이지만 저장본이 있으면 "오래됨" 검사가 먼저 걸린다) |
| 현재보다 큰 번호 (`99`) | 400 `INVALID_REQUEST` "콜백 버전이 현재 요청 버전의 범위를 벗어났습니다." |
| 같은 번호 재전송 (`2`, ID는 엉터리 `[999999]`) | 200 — 재저장 없음. 같은 번호는 ID 검사 없이 넘어간다 |
| 경로 1 ≠ 본문 `recipientUserId: 7` | 400 `RECIPIENT_ID_MISMATCH` |
| `profileStatus: "FAILED"` | 400 `INVALID_REQUEST` "입력값을 확인해 주세요." |
| 없는·삭제된 상품 ID (새 번호에서) | 400 `INVALID_REQUEST` — **전체 거부**. 새 번호가 PENDING일 때만 검사가 걸린다(§7 D1 중에 시험 가능) |

## 7. 시험 D — 장애 재현

### D1. AI가 꺼진 동안 취향 변경 (BE의 7.6 실패 처리)

1) 터미널 A에서 AI를 Ctrl+C로 내린다. 2) 사용자 2로 §5의 로그인·저장을 한다. 3) BE 행을 3초마다 본다:

```bash
for i in $(seq 1 15); do docker exec seonjalal-mysql-local sh -c 'mysql -ugift -p"$MYSQL_PASSWORD" -D gift -N -e "select sleep(3); select concat(date_format(now(6),\"%T\"), \"  \", profile_status, \"  v=\", source_version, \"  analyzed=\", analyzed_source_version, \"  retry=\", retry_count) from recipient_profiles where recipient_id=2;"' 2>/dev/null | grep -v '^0$'; done
```

10-06에 본 것: 10초 틱마다 `source_version`이 1→2→3→4→5→6으로 오르고 `retry_count`는 0 그대로, BE 로그에 틱마다 `AI 프로파일링 요청에 재시도 가능한 오류 … type=COMMUNICATION_ERROR`. 4) AI를 다시 올리면(2-2) 다음 틱에 **최신 번호**로 7.6이 나가 바로 `COMPLETED`가 된다. 1~5번은 AI가 본 적 없는 번호로 남는다.

### D2. AI는 접수하지만 7.7이 BE에 못 닿을 때 (PENDING 고착)

AI를 콜백 주소만 틀리게 올린다(8089번에는 아무것도 없다):

```bash
cd /Users/emet/KTB4-12th/KTB4-12th-AI/profiling && PROFILING_SERVICE_TOKEN=local-profiling-token PROFILING_BACKEND_BASE_URL=http://localhost:8089 PROFILING_CATALOG_SOURCE=db uv run uvicorn profiling.main:app --port 8000
```

사용자 1로 비선호를 저장하고 D1의 관찰 명령(`recipient_id=1`)을 돌린다. 10-06에 본 것:

- AI 로그: `7.7 콜백 … CALLBACK_UNREACHABLE … Connection refused` → `0.5초 뒤 다시 보낸다 (2/3)` → `2.0초 뒤 다시 보낸다 (3/3)` → `시도 3/3 → RESULT_READY`. `/health`의 `store.undelivered`가 1.
- BE: `PENDING`, `pending_since` 기록. 이 상태에서 **다시 비선호를 바꿔도 새 7.6이 안 나간다**(`last_changed_at`만 쌓인다). AI를 정상 주소로 되돌려도 BE는 그대로 PENDING이다 — 타임아웃이 아직 없어서.

풀어 주는 법 — AI가 보관한 본문을 BE에 직접 보낸다(번호는 PENDING인 번호로):

```bash
BODY=$(docker exec profiling-ai-db psql -U ai_user -d ai_chat -At -c "select callback_payload::text from ai_profile.profile_runs where recipient_user_id=1 and source_version=3") && curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8080/api/internal/v1/recipients/1/profile -H 'Authorization: Bearer local-profiling-token' -H 'Content-Type: application/json' -d "$BODY"
```

200이 오면 BE가 `COMPLETED`로 바뀌고, 밀려 있던 변경이 다음 틱에 새 번호로 나간다(10-06: v3 완료 직후 v4가 나가 `COMPLETED 4/4`). 그냥 지우려면 §3 초기화.

## 8. 확인 SQL·로그 모음 · 끄기 · 자주 걸리는 것

**BE 상태**

```bash
docker exec seonjalal-mysql-local sh -c 'mysql -ugift -p"$MYSQL_PASSWORD" -D gift -t -e "select recipient_id, profile_status, source_version, analyzed_source_version, retry_count, last_changed_at, pending_since from recipient_profiles order by recipient_id; select recipient_id, source_version, count(*) n, min(rank_order) rmin, max(rank_order) rmax from recipient_recommended_products group by 1,2;"'
```

**추천 30개의 대분류 분포** (비선호 대분류가 0이어야 한다):

```bash
docker exec seonjalal-mysql-local sh -c 'mysql -ugift -p"$MYSQL_PASSWORD" -D gift -t -e "select c.parent_id, count(*) from recipient_recommended_products r join products p on p.id=r.product_id join categories c on c.id=p.category_id where r.recipient_id=1 group by c.parent_id order by 1;"'
```

**AI 상태**

```bash
docker exec profiling-ai-db psql -U ai_user -d ai_chat -c "select recipient_user_id, source_version, status, callback_attempts, attempt, error->>'code' as code, jsonb_array_length(callback_payload->'recommendedProductIds') as ids from ai_profile.profile_runs order by updated_at desc limit 10;"
```

`curl -s localhost:8000/health`의 `store.undelivered`(전달 못 한 결과 수)와 `supervisor`(큐·처리 수)도 같이 본다.

**로그에서 찾는 말** — AI: `7.6 접수` · `중복 판정` · `RESULT_READY` · `7.7 콜백` · `미전달` · `CALLBACK_UNREACHABLE` · `CONTRACT_7_7_REJECTED`. BE: `path=/api/internal/v1/recipients/{recipientUserId}/profile, status=` · `재시도 가능한 오류` · `재시도할 수 없는 오류` · `STALE_SOURCE_VERSION`.

**끄기** — 터미널 A·B에서 Ctrl+C. 컨테이너는 둬도 되고, 내리려면:

```bash
docker stop seonjalal-mysql-local profiling-ai-db
```

**자주 걸리는 것**

| 증상 | 원인 · 조치 |
|---|---|
| 비선호를 저장했는데 7.6이 영영 안 옴 | BE 기동 명령에 `AI_PROFILE_SCHEDULING_ENABLED=true`가 빠짐. BE 로그에 10초마다 `recipient_profiles` 쿼리가 없으면 이것 |
| 7.6은 왔는데 7.7이 401 | 토큰 불일치. AI의 `PROFILING_SERVICE_TOKEN`과 BE `.env`의 `PROFILING_SERVICE_TOKEN`이 같아야 한다 |
| 7.6이 503 `활성 카탈로그가 없습니다` | AI DB에 카탈로그 없음 → §2-2 적재 |
| 한 사용자가 PENDING에서 안 움직임 | §7 D2 상태. §7의 "풀어 주는 법" 또는 §3 초기화 |
| BE가 10초마다 번호만 올림 | AI가 꺼져 있음(§7 D1). AI를 올리면 다음 틱에 복구 |
| 포트 8000·8080이 이미 사용 중 | `lsof -nP -iTCP:8000 -iTCP:8080 -sTCP:LISTEN`으로 찾아 끈다 |
| `docker` 명령이 데몬에 못 붙음 | Docker Desktop을 켠다 |
| BE 첫 기동이 오래 걸림 | Gradle 컴파일 + Flyway 마이그레이션. 1~2분 기다린다 |

## 9. 10-06에 본 "정상" 값 (비교 기준)

| 항목 | 값 |
|---|---|
| 7.6 접수 → 7.7 전달 | 약 0.1초 (분석 ~20ms + 콜백 50~80ms) |
| BE 콜백 처리 | 48~81ms |
| 비선호 저장 → 7.6 발송 | 12초 (디바운스 10초 + 틱 ≤10초) |
| 추천 개수 · 순위 | 30개, `rank_order` 1~30, 옛 번호 행은 삭제됨 |
| 비선호 대분류 포함 | 0개 |
| AI `/health` | `catalog.products 4231` · `store.undelivered 0` · `supervisor.slots 1, queue_max 200` |
| BE 자동 시험 통과 | 정상 경로(T1·T3) 오류 0건 · 오류 응답 7종 §6 표와 같음 |
