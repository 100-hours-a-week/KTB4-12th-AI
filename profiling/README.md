# profiling — 수신자 프로파일링 (AI 백엔드 · Profile / Catalog 파트)

Backend 가 받는 사람의 **비선호 카테고리 · 취향 문장 · 최근 리뷰**를 **7.6** 으로 보내면, 즉시 `202` 로 접수하고 백그라운드에서
① 리뷰 별점으로 소분류 선호를 계산하고 ② LLM 으로 취향 태그를 뽑아 검증한 뒤 ③ 상품 30개를 골라 **7.7 콜백**으로 돌려준다.
태그 · 리뷰 지문은 AI DB 가 보관하고, Backend 에는 상품 번호만 보낸다(DR-035).

| 상태 (2026-10-13 · 브랜치 `feat/v2-profiling-merge`) | |
|---|---|
| 동작 범위 | **v2 코어** — 리뷰 별점 편향 + LLM 태그(가드레일 · 검증기) + 30개 고르기 D(비선호 하드 제외 · 리뷰한 상품 제외 · 소분류 상한 10 · 같은 모델 하나 · 태그가 있으면 임베딩 검색 + MMR). 입력 세 칸이 전부 비면 추천 0개(결정 C) |
| 태그 추출 | **v2 = 외부 API(OpenRouter) · 3축 규격 `tag3`**(카테고리 · IP · 브랜드) — DR-037 · DR-038. 로컬 LLM(Ollama)은 v3. 추출기를 끄면(`TAGGER=none`) 별점 편향 + D 만 돈다. LLM 이 실패해도 요청은 실패하지 않는다(태그 없이 30개) |
| BE 연동 | 실물 BE `develop`(10-13 `55dc526`)와 v2 설정으로 처음부터 끝까지 확인 — 디바운스 → 7.6 → 태그 → 30개 → 7.7 → BE `COMPLETED`, BE 30 = AI 30(집합 · 순서) · 비선호 0 · 리뷰한 상품 0. BE 통합 수정점 v0.7 동작(틱마다 `/health` 핑 · 복구 전송 · PENDING 중 새 번호)은 10-11 여섯 시나리오 통과 |
| 저장소 | **PostgreSQL 하나뿐** — `ai_profile.profile_runs`(실행 기록) · `recipient_profiles`(수신자 프로필) · `recipient_reviews`(리뷰 지문, `0005`) · `ai_catalog`(상품 · 카테고리 · 적재 버전). DB 없이 뜨는 모드는 없다 |
| 시험 | `uv run pytest -q` → **399 passed, 2 skipped**(단위 333 · 통합 66, DB 꺼져 있으면 통합 skip) |
| 문서 | **비공개(10-13).** 설계 · 시험 결과 · 결정 기록은 팀 비공개 폴더에서 관리하고 이 저장소에는 코드 · 시험 · 이 README · `이름_대조표.md` 만 둔다. 아래에 링크 없이 나오는 문서 이름은 그 비공개 문서다 |
| 담당 | Profile · Catalog · DB adapter · Embedding adapter. Chat · Search · Runtime 은 팀원 |

---

## 1. 빠른 시작

```bash
cd profiling
uv sync                                       # .venv + 의존성 (uv.lock 그대로)
docker compose up -d ai-db                    # 로컬 PostgreSQL 16 + pgvector (Docker Desktop)
uv run alembic upgrade head                   # 0001 ~ 0005
uv run pytest -q                              # 399 passed, 2 skipped
uv run ruff check src tests tools alembic
```

`.env` 는 저장소에 없다(예시 파일도 두지 않는다). 키 목록과 기본값은 `src/profiling/settings.py` 가 정본이고, 담당자에게 받은 값을 `profiling/.env` 에 둔다.

### 띄우기

```bash
# 가짜 Backend + 시험 콘솔 (:8081)
uv run uvicorn tools.fake_backend.app:app --port 8081
# AI 앱 (:8000) — 태그 끔(가장 단순)
PROFILING_SERVICE_TOKEN=dev-token uv run uvicorn profiling.main:app --port 8000
```

**v2 운영 설정**(외부 API · 3축 · 임베딩) — OpenRouter 키는 `profiling/.env` 의 `PROFILING_OPENROUTER_API_KEY`:

```bash
PROFILING_CATALOG_SOURCE=db PROFILING_DISLIKE_MODE=exclude PROFILING_POOL_STRATEGY=d \
PROFILING_TAGGER=openrouter PROFILING_TAGGER_MODEL=<OpenRouter 모델 id> PROFILING_TAG_SPEC=tag3 \
PROFILING_TAGGER_TIMEOUT_S=30 PROFILING_EMBEDDING_DIR=../product-search \
PROFILING_BACKEND_BASE_URL=http://localhost:8080 PROFILING_SERVICE_TOKEN=<BE 와 같은 값> \
uv run uvicorn profiling.main:app --port 8000
```

기동 로그에서 확인: `카탈로그 로드 source=db … products=4231` · `태그 규격 tag3 축=카테고리·IP·브랜드 판=tag3-…` · `태그 추출기 openrouter model=…` · `임베딩 …`.
`curl localhost:8000/health` → `status ok` · `catalog.active true` · `store.connected true` · `store.migration "0005"`.

### 컨테이너로 (배포와 같은 모양)

```bash
docker compose up -d --build                  # ai-db(healthy) → ai-migrate(alembic 한 번) → ai-app(:8000)
docker compose run --rm ai-migrate python -m tools.catalog.load_catalog --package /경로/product-catalog-...   # 카탈로그 적재(일회성)
```

이미지에는 `src/` · `alembic/` · 운영 CLI `tools/catalog` 만 들어간다(`.dockerignore` — 시험 · 문서 · 시험 도구 · 시험 결과물 제외).

---

## 2. 요청 한 건의 흐름

1. **접수(HTTP)** `POST /api/internal/v1/ai/profile/extract-and-pool` → Pydantic 검증(위반 `400 INVALID_REQUEST`) → 서비스 토큰(`401`) →
   활성 카탈로그 없으면 `503`(Retry-After 300) → Supervisor 큐에 넣기(가득이면 `503`, Retry-After 30) → **`202 PENDING`**.
2. **판정(워커 스레드)** `dispatch` — 수신자 · 버전 잠금 → `decide()`: 처음이면 분석 · 같은 본문이 이미 결과 있으면 재전송만 · 실행 중이면 아무것도 안 함.
3. **분석** `pipeline.profile()`
   1. 실행 기록 `RUNNING` 저장 → 활성 카탈로그 한 번 잡기(요청 안에서 같은 버전)
   2. 리뷰 별점 → **소분류 편향**(`category_scores`)
   3. 취향 문장 · 리뷰가 있고 추출기가 켜져 있으면 **태그 추출**(`_extract_tags`): 가드레일(`guard.clean` — 개인정보 · 욕설 가림, 의미 없는 글 · 인젝션 의심 글 버림) →
      근거(취향 문장 · 4~5점 리뷰)가 없으면 LLM 을 부르지 않음 → LLM 한 번(태그 규격의 시스템 프롬프트 · JSON 스키마) → **검증기**(규격의 축만 · 근거 ref 가 실제 취향/4~5점 리뷰 ·
      판촉 단어 · 욕설/개인정보/문장형 태그 버림 · 중복 제거)
   4. **30개 고르기** — 입력 세 칸이 전부 비면 0개(결정 C). 아니면 D: 비선호 대분류 하드 제외 · 리뷰한 상품 제외 · 소분류 최대 10 · 같은 브랜드+모델 코드 하나 ·
      태그와 임베딩이 있으면 태그마다 비슷한 상품 50개 → 점수(유사도 + 인기 + 편향) → MMR(λ 0.7). 모자라면 감점 모드에서만 비선호로 채움
   5. 결과 `RESULT_READY` 저장(콜백 본문을 먼저 커밋) → 수신자 프로필 · 리뷰 지문 upsert(낮은 버전은 무시)
4. **콜백** `POST 7.7` → `200 DELIVERED` · `409 SUPERSEDED` · `4xx FAILED` · `5xx/네트워크`는 같은 슬롯에서 최대 3번(0.5초 · 2초 뒤). `FAILED` 는 콜백하지 않는다(Backend 가 복구 전송으로 다시 묻는다).

모든 단계는 원문(리뷰 글 · 취향 문장 · LLM 출력)을 로그에 남기지 않는다 — 태그 단계는 한 줄 `tag_extract <상태> recipient= … prompt=<판> … in_tok= out_tok= llm_ms= cost_usd= kept= dropped= axes= drops=`.

---

## 3. 구조

**Ports & Adapters.** 업무 코드는 바깥(HTTP · DB · LLM · 임베딩)을 모르고, 바깥이 `ports.py` 의 모양에 맞춰 들어온다. 그래서 단위 시험은 가짜 구현만으로 돈다.

| 모듈 (`src/profiling/`) | 역할 |
|---|---|
| `main.py` | 앱 조립 · lifespan(DB · 카탈로그 · 태그 규격 · 추출기 · 임베딩 · Supervisor · 끊긴 실행 정리) · `/health` · 오류 응답 |
| `intake.py` | 7.6 접수 · 토큰 · 큐 넣기 · `dispatch`(잠금 · 판정) · 콜백 보내고 기록 |
| `supervisor.py` | 워커 스레드(`PROFILING_SLOTS`) + 상한 큐(`QUEUE_MAX`) |
| `pipeline.py` | 한 건 분석 — 편향 · 태그 단계 · 결정 C · 30개 · 저장 순서 |
| `selection.py` | 30개 고르기 D(제외 · 상한 · 중복 제거 · 임베딩 + MMR) |
| `tagging.py` | **태그 규격(`TagSpec`)** · 프롬프트 만들기 · 검증기 · 저장형 변환. 시스템 프롬프트는 `prompts/<규격>_system.md` |
| `guard.py` | 입력 가드레일(정리 · 개인정보 · 욕설 · 의미 없음 · 인젝션) · 태그 검사. 판 `g4` |
| `llm.py` | 태그 추출 어댑터 — `OllamaTagExtractor` · `OpenRouterTagExtractor`(키는 헤더에만, 실패는 종류별 `TagExtractError`) |
| `embedding.py` | product-search 상품 벡터 + 질의 인코더 어댑터 |
| `catalog.py` · `stores.py` · `backend.py` | 카탈로그(파일 · DB) · PostgreSQL 저장소 · 7.7 HTTP 어댑터 |
| `schemas.py` · `types.py` · `ports.py` · `settings.py` | HTTP DTO(camelCase) · 내부 타입(snake_case) · 포트 · 설정(`PROFILING_*`) |

`alembic/versions/` — `0001` recipient_profiles · `0002` profile_runs · `0003` ai_catalog · `0004` 열 주석 · `0005` recipient_reviews.

**이름 규칙.** HTTP 경계(`schemas.py`)만 camelCase, 나머지는 snake_case. 변환은 `pipeline.to_internal()`(7.6 → 내부) · `backend.to_callback()`(내부 → 7.7) 두 곳뿐. 세부는 [이름_대조표.md](이름_대조표.md).

### 태그 규격 바꾸기 (3축 ↔ 4축)

`PROFILING_TAG_SPEC=tag3`(카테고리 · IP · 브랜드, v2 운영) 또는 `tag4`(+ 특징, 코드 기본값) 한 줄 + 재시작. 축 · 프롬프트 · 스키마 · 판 이름(`tag3-…`/`tag4-…`)이
한 묶음으로 바뀐다. 프롬프트 파일에 적힌 축이 규격과 다르거나 모르는 이름이면 **서버가 뜨지 않는다**. 문구만 바꿔 시험할 때는 `PROFILING_TAG_PROMPT_FILE`(축은 규격을 따른다).

---

## 4. 주요 설정 (`PROFILING_*`)

| 설정 | 기본값 | 뜻 |
|---|---|---|
| `DATABASE_URL` | 로컬 compose 값 | PostgreSQL. 연결 실패면 앱이 뜨지 않는다 |
| `CATALOG_SOURCE` | `file` | `db` 가 배포 경로(`ai_catalog`). `file` 은 예시 111건(`tests/fixtures/catalog_sample.json`) |
| `BACKEND_BASE_URL` · `SERVICE_TOKEN` | fake_backend · 빈 값 | **빈 토큰이면 7.6 토큰 검사를 건너뛴다 — 로컬 전용.** 개발 · 운영 서버는 반드시 넣는다 |
| `TAGGER` · `TAGGER_MODEL` · `TAGGER_TIMEOUT_S` | `none` · `gemma4:12b` · 120 | 태그 추출기(`none` · `ollama` · `openrouter`). 외부 API 는 상한 30초 권장 |
| `TAG_SPEC` · `TAG_PROMPT_FILE` | `tag4` · 없음 | 태그 규격(위) |
| `OPENROUTER_API_KEY` · `OPENROUTER_URL` · `OLLAMA_URL` | — | 키는 `.env` 에만(SecretStr — 로그 · repr 에 안 찍힘) |
| `EMBEDDING_DIR` | 없음 | product-search 폴더. 주면 D 의 임베딩 단계가 켜진다 |
| `DISLIKE_MODE` · `POOL_STRATEGY` · `POOL_SIZE` | `exclude` · `d` · 30 | 비선호 제외/감점 · 고르기 방식 · 개수 |
| `PROFILING_SLOTS` · `QUEUE_MAX` | 1 · 200 | 동시 분석 수 · 대기열 상한(넘으면 503). LLM 을 켜면 건당 시간 × 대기열이 BE 의 '분석 중' 시간 초과보다 짧아야 한다 |
| `CALLBACK_MAX_ATTEMPTS` · `RUNNING_STALE_S` | 3 · 300 | 7.7 재시도 · 끊긴 실행 정리 기준 |

---

## 5. 계약과 정해진 규칙

| 규칙 | 값 | 근거 |
|---|---|---|
| 7.6 입력 | 비선호 ≤5 · 리뷰 ≤10(11개면 400, 자르지 않음) · 별점 1~5 · 글 null 가능. 값 필수는 `recipientUserId` · `sourceVersion`. **1단계(지금)** `dislikedCategories` 키 필수 · **2단계(BE V2 배포 시)** `giftPreference` · `reviews` 도 키 필수 | 요청서 v1.2 · 10-09 D4 |
| 7.7 | 상품 번호 ≤30 · `sourceVersion` 만. 태그는 보내지 않는다 | DR-035 |
| 입력이 전부 비면 | 추천 0개 콜백 — BE 는 0행 저장 + 인기순 대체 | 결정 C(10-09) |
| 비선호 | D 에서 하드 제외(감점 모드는 30개를 못 채울 때만 감점 순서로 채움). 운영 감점 전환은 BE V2 배포 시 | 10-12 · 10-11 |
| 리뷰한 상품 | 추천하지 않는다(별점 · 태그는 취향 신호로만) | 10-12 |
| 같은 번호 재요청 | 재분석 없이 재전송 · 더 새 번호가 전달되면 옛 미전달 행 `SUPERSEDED` | 통합 수정점 v0.7 |
| 계약 불일치 | HTTP 코드는 계약대로, 원인은 내부 코드 `ErrorCode`(`CONTRACT_7_6_UNKNOWN_FIELD` · `CONTRACT_7_7_REJECTED` · `CONTRACT_7_9_SCHEMA`) | 09-22 |

---

## 6. 시험과 도구

원칙: **업무 코드는 가짜 구현으로, 어댑터는 가짜 바깥(`httpx.MockTransport`)으로, 계약은 스키마로.** 단위(`tests/unit`)는 DB · 네트워크 · LLM 없이 돌고,
통합(`tests/integration`)은 진짜 PostgreSQL(꺼져 있으면 skip) — 마이그레이션 · 저장소 · 앱 끝에서 끝 · 30개 콜백 시나리오.

| 도구 (`tools/`) | 하는 일 |
|---|---|
| `scenario_callback30/` | **30개 콜백 하네스** — 가상 수신자 엑셀(정상 10 · 비정상 12) × 두 모드를 앱 전체로 돌려 자동 채점(필수 검사 = 계약 · 비선호 0 · 재현성 · 가드레일, 관찰 검사 = 모델 품질). `--tagger ollama\|openrouter --tagger-model … --tag-spec tag3 --embedding-dir ../product-search`. [README](tools/scenario_callback30/README.md) |
| `embed_compare/` | 태그 추출 모델 비교 — `extract_tags`(모델별 추출 · 시간 · 토큰 · 비용 · 3회 반복 안정성, `--openrouter` `--tag-spec` `--label`) · `compare_taggers` · `compare_tag_sources` · 임베딩 방식 비교 `run.py`. [README](tools/embed_compare/README.md) |
| `bench/` | `gpu_monitor`(GPU · 메모리 압박 · 스왑 · Ollama CPU 밀림 · 프로세스별 CPU · AI 대기열을 1초 CSV + 그림) · `percentiles` · `trace_selection` · `llm_service_report` |
| `loadtest/` | 7.6 동시 N건 부하 하네스(`/health` 지연 · 큐 · 503 · DB 활동). [README](tools/loadtest/README.md) |
| `be_integration/` | 실물 BE 와 붙이는 시나리오 드라이버 `drive_v07.py`(정상 · AI 꺼짐 · 콜백 불통 · 토큰 틀림 · AI DB 꺼짐) |
| `fake_backend/` | 가짜 Backend(7.7 수신 · 실패 주입 · 7.9 · 시험 콘솔 :8081). 생애주기는 옛 09-27 모델 — 7.6/7.7 모양 확인용 |
| `catalog/` | **운영 CLI** — `load_catalog`(BE 전달 패키지 → `ai_catalog`) · `import_be_ids` · `fetch_export`(7.9 점검) |

외부 API 를 부르는 시험(`--tagger openrouter` · `extract_tags --openrouter`)은 **유료**다. 키가 없으면 추출기는 꺼진 채로 뜬다.

---

## 7. 배포 · 보안 주의

- **토큰.** `SERVICE_TOKEN` 이 비면 토큰 검사를 건너뛰고, compose 기본값 `dev-token` 은 공개된 값이다 — 개발 · 운영 서버는 반드시 다른 값을 넣는다(앱 기동 검사는 다음 할 일).
- **키 · `.env`.** 저장소에 두지 않는다(`.gitignore`). OpenRouter 키는 헤더로만 보내고 로그 · 예외 · repr 에 남기지 않는다.
- **외부로 나가는 것.** LLM 에는 가드레일을 거친 프롬프트만 간다. 원문은 로그에 없다. LLM 추적 도구(LangSmith 등)는 로컬 · 개발 전용, 운영 금지(DR-036).
- **이미지.** 시험 · 문서 · 시험 도구 · 시험 결과물은 넣지 않는다(`.dockerignore`). 비루트(uid 10001).

---

## 8. 다음 할 일

| # | 일 | 곳 |
|---|---|---|
| 1 | **카테고리 태그 사전 확인** — 3축에서 막연한 말('실용적인 선물')이 카테고리로 나오는 것을 막고 운영에 3축을 켠다(DR-038 조건) | `tagging.validate` · 카탈로그 소분류 · 상품 종류 |
| 2 | **추천 선택 가중** — IP · 브랜드 · 카테고리 축 가중(또는 IP 몫). 리뷰 상품의 카테고리 태그가 캐릭터 취향을 밀어내는 문제 | `selection.AXIS_WEIGHT` · `_embedding_pick` |
| 3 | **운영 기동 검사** — `PROFILING_ENV=prod` 에서 빈 토큰 · `dev-token` 이면 기동 실패 | `settings.py` · `main.py` |
| 4 | **부하 시험(LLM 흉내)** — 외부 API 응답 시간을 흉내 낸 추출기로 슬롯 · 대기열 · 틱 크기 확정 | 브랜치 `test/v2-profiling-loadtest` |
| 5 | **0006 마이그레이션** — 프롬프트 판 · 축 · 리뷰별 추출 결과 저장(캐시) | `alembic` · `stores.py` · `types.py` |
| 6 | 외부 LLM 모델 확정(3축 기준 재비교) · 리뷰 캐시 | 운영 설정 |
| 7 | BE V2 배포 시 — 7.6 키 규칙 2단계 · 비선호 감점 모드 전환 | `schemas.py` · 운영 설정 |
| 8 | 챗봇 프로필 조회 API(원문 + 분석 결과, 상품은 번호만) | `intake.py` 또는 별도 라우터 |
| 9 | 팀원 앱과 합치기 — 라우터 · 어댑터만 옮긴다 | `main.py` · 설정 이름 |
