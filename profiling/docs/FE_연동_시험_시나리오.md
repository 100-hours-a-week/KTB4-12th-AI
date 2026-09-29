# FE 연동 시험 시나리오 (2026-09-25)

FE가 보는 화면은 **Backend의 `profileStatus`와 상품 목록 정렬**이다. AI는 FE와 직접 말하지 않는다(7.6·7.7은 내부 API). 그래서 이 문서는 **"AI의 어떤 시퀀스가 화면의 무엇이 되는가"**를 시나리오로 적고, 그것을 **실물 BE 없이 로컬에서 먼저 돌리는 방법**을 함께 적는다.

| | |
|---|---|
| 층 | L1 AI 단독(단위 155 · 통합 51) · **L2 BE↔AI(이 문서)** · L3 FE→BE→AI(실물 연동 때) |
| 도구 | `tools/fake_backend` — 09-25 합의(디바운스 → 7.6 → 202 → 타임아웃 시 같은 번호 재전송 2회 → FAILED)를 그대로 구현했다 |
| 시간 | 실제 BE는 디바운스 1시간·타임아웃 10분. 여기서는 **초 단위로 줄여** 한 시나리오가 30초 안에 끝난다 |
| 확인 | AI `/health`·로그·`profile_runs` · BE(fake) `/console/be/state`·`/console/be/events` |

## 0. 띄우기

```bash
cd profiling && docker compose up -d && uv run alembic upgrade head

# 가짜 Backend — 디바운스 3초 · PENDING 타임아웃 6초 · 재전송 2회
FAKE_BACKEND_DEBOUNCE_S=3 FAKE_BACKEND_PENDING_TIMEOUT_S=6 \
  uv run uvicorn tools.fake_backend.app:app --port 8081

# AI — 카탈로그는 DB(배포와 같은 경로)
PROFILING_CATALOG_SOURCE=db PROFILING_BACKEND_BASE_URL=http://localhost:8081 \
  uv run uvicorn profiling.main:app --port 8000
```

시나리오 사이에는 `curl -X DELETE localhost:8081/console/be/state` 로 초기화한다.

## 1. 시나리오

각 시나리오의 **FE 대응**은 실물 연동 때 화면에서 확인할 것을 적은 것이다.

### S1 · 성공 — 비선호 저장 → 추천 30개

| | |
|---|---|
| FE 대응 | 수신자 비선호 저장 → 잠시 후 상품 목록(`sort=AI_RECOMMENDED`)에 30개가 앞으로, 비선호 카테고리는 안 보임 |
| 조작 | `POST /console/be/change {recipientUserId, dislikedCategories:[{categoryId:1,categoryName:"뷰티"}]}` — 실제 BE처럼 **대분류**로. 추천 30개에 뷰티 소분류(스킨케어·메이크업·향수·…)가 하나도 없어야 한다 |
| 기대 | 디바운스 뒤 7.6 → 202 → PENDING → 7.7 → **COMPLETED**, 추천 30개에 소분류 12 없음 |
| 확인 | `/console/be/state` → `profileStatus=COMPLETED`, `analyzedSourceVersion == sourceVersion` · AI `profile_runs.status=DELIVERED` |

### S2 · 콜드스타트 — 아무것도 저장하지 않은 수신자

| | |
|---|---|
| FE 대응 | 개인화 없음 → 인기순 등 대체 정렬 |
| 조작 | 아무것도 하지 않는다 (상태만 만들어 둔다) |
| 기대 | 7.6이 **한 번도 나가지 않음** · `profileStatus=NONE` · `sourceVersion=0` |

### S3 · 분석 중 — 202 직후

| | |
|---|---|
| FE 대응 | 이전 결과가 있으면 그것을 계속 보여줌(없으면 대체 정렬). "분석 중" 표시는 선택 |
| 조작 | S1과 같되 202 직후에 상태를 읽는다 |
| 기대 | `profileStatus=PENDING` · `pending_since` 기록 · 잠시 뒤 COMPLETED |

### S4 · AI 장애 — 활성 카탈로그 없음(503)

| | |
|---|---|
| FE 대응 | 화면이 깨지지 않고 이전 결과 유지 |
| 조작 | `update ai_catalog.catalog_versions set is_active=false` → 비선호 변경 |
| 기대 | 7.6 → **503**, 상태 **그대로** · BE는 `Retry-After` 뒤 **같은 번호**로 재시도(`retryCount` 1) · 카탈로그를 되살려 두면 그 재시도가 202 → COMPLETED. 로컬은 AI `.env`에 `PROFILING_RETRY_AFTER_S=5` |
| 확인 | AI `/health` → `catalog.active=false` · BE 이벤트에 `7.6 신규 → 503`(retryAfter 5) 다음 `7.6 재시도1 → 202` · `sourceVersion` 그대로 |
| 주의 | AI는 활성 id를 1초에 한 번만 다시 묻는다(`PROFILING_CATALOG_POLL_TTL_S`, 기본 1). `is_active=false` 뒤 최대 1초는 아직 202가 나올 수 있고 503은 그 뒤부터. 되살린 뒤 회복도 같은 1초 안 |

### S5 · 콜백 유실 → 같은 번호 재전송 (09-25 합의의 핵심)

| | |
|---|---|
| FE 대응 | 잠깐 반영이 늦을 뿐, 사용자가 다시 손대지 않아도 결국 반영됨 |
| 조작 | `PUT /console/mode {"mode":"500"}` → 비선호 변경 → 첫 7.7이 5xx로 실패 → 타임아웃 뒤 재전송 → `mode=ok` 로 복구 |
| 기대 | AI가 먼저 같은 슬롯에서 0.5초·2초 뒤 두 번 더 보내고(3회) 그래도 5xx면 보관 · BE `retryCount` 1 → (필요시 2) · **BE 재시도 때 AI는 재분석하지 않고 저장된 결과를 재전송** · 최종 COMPLETED |
| 확인 | AI 로그 `7.7 미전달 … 다시 보낸다 (2/3)` · `중복 판정 … → resend` · `profile_runs.attempt=1`, `callback_attempts ≥ 4`(즉시 3회 + 재전송) |

### S6 · 재시도 소진 → FAILED

| | |
|---|---|
| FE 대응 | 이전 결과가 있으면 그것, 없으면 대체 정렬. "분석 실패"는 사용자에게 보이지 않아도 된다 |
| 조작 | `mode=500` 을 유지한 채 비선호 변경 |
| 기대 | 같은 번호 재시도 2회를 다 쓰고 `profileStatus=FAILED` · **AI는 FAILED를 보내지 않는다**(BE 판정) |

### S7 · 순서 역전 → 409

| | |
|---|---|
| FE 대응 | 낡은 결과가 최신을 덮지 않음 |
| 조작 | S1으로 COMPLETED(v=1)를 만든 뒤, `POST /console/send-7.6?auto_source_version=false` 로 **더 낮은 번호**를 AI에 직접 접수 |
| 기대 | AI가 그 번호로 7.7 → BE **409 STALE_SOURCE_VERSION** → AI `profile_runs.status=SUPERSEDED` · BE 추천 목록 그대로 |

## 2. 실물(FE·BE)로 옮길 때

| 여기(로컬) | 실물 |
|---|---|
| `POST /console/be/change` | FE에서 비선호 저장 |
| 디바운스 3초 | BE 디바운스 1시간(최대 6시간) — **스테이징에서는 줄여 달라고 요청해야 한다** |
| 타임아웃 6초 | BE PENDING 타임아웃 10분 |
| `mode=500` 실패 주입 | BE 장애를 인위로 만들기 어렵다 → S5·S6은 로컬에서만 온전히 재현된다 |
| `/console/be/state` | BE `profiles` 행 조회 |

**S1·S2·S3·S4·S7은 실물에서도 그대로 확인할 수 있고, S5·S6은 로컬(이 문서)로 대신한다.**
