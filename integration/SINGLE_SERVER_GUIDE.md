# 단일 서버 통합 후보 실행 가이드

이 문서는 통합 릴리스 후보의 설정·책임 경계다. [Quickstart](QUICKSTART.md)에서 시작하고 [복구 절차](RECOVERY.md)와 [팀 확인 사항](RELEASE_READINESS.md)을 확인한다. 이전 후보와 실행 증거는 저장소 밖에 보존한다. 현재 후보를 배포하거나 기존 데모를 바꾸지 않았다.

[에멧 변경 근거](EMMET_PR15_CHANGES.md), [ERD·사전 생성 파일 호환성](PRECOMPUTED_CATALOG_COMPATIBILITY.md)을 함께 읽는다. 별도 Chat 공유 브랜치는 취소되었으며 이 후보의 배포 경로가 아니다.

## 기준 소스

| 구성 | 기준 |
|---|---|
| 에멧 | 병합된 [PR #15](https://github.com/100-hours-a-week/KTB4-12th-AI/pull/15), 병합 전 head `21b85a351fe2cc344398febb6cd7fd418e77e54a` |
| 현재 branch base | PR15 squash 병합 후 `develop`, `bfce5219ef622a1634569e3b92b879d10e51e9b2` |
| 보존한 동결 후보 | `f8821e6b593b7cc751e064ea46ca6c9202da6bde`, tree `130faaa0c354a3b159f04046c4591627f4fbf94b` |
| PC 하네스 | 승인된 SSH PC의 `/home/johnas/product-agent-lab`, `main`, `1ea3cdef` 및 당시 미커밋 파일 스냅샷. 채택 버전 `stable-v4.12` |
| 외부 Chat 계약 어댑터 | `IntegratedHarness`, `stable-v4.12+ai-be-v1`, 카드 최대 3개와 요청한 더 적은 수 |
| DB | PostgreSQL 18, Emmet Alembic `0011` + 통합 `single-server/1` |

PC·에멧 원본과 사용 중인 run-2는 수정하지 않았다. PC 미커밋 변경을 버리지 않고 파일별 해시로 보존했다. 단순 파일 병합 대신 Chat 표시 계약, Search 세대 API, Profiler 정책 어댑터를 분리했다.

## 책임과 데이터 경계

| 프로세스 | 책임 | 직접 SQL |
|---|---|---|
| Chat | 대화, 도구 실행, 최대 3개 `productId/reason`, SSE·멱등성·취소·소유권 | `ai_chat` |
| Profiler | 취향 해석, 비선호, 후보 30개, MMR·재랭킹, 개인 프로필·콜백 | `ai_profile` |
| Search | 상품·태그·벡터 세대, 정확 벡터 검색, 기존 hybrid 검색, 현재 상품 가시성 | `ai_catalog`, QA용 `ai_search` |
| catalog-sync | BE 완전 export 검증, 변경 상품 임베딩, 새 세대 준비·pending 제안 | `ai_catalog` 제한 쓰기 |
| migrate | 명시적 DB 구조·권한 마이그레이션 | 전용 준비 DB의 스키마 소유자 |

상품 정본은 `ai_catalog.generations`의 동결 JSON·manifest·벡터 payload다. 읽기 투영 `generation_products`도 제공한다. 파일과 RAM 인덱스는 재생성 가능한 캐시다. 별도 상품 복제 DB를 만들지 않는다. 개인 프로필을 Search에 넣지 않는다. Chat과 Profiler는 상품·검색·임베딩을 공용 API로 읽는다. Emmet의 기존 pgvector 마이그레이션 코드는 보존했지만 통합 검색 알고리즘을 pgvector로 바꾸지 않았다. 큐/RabbitMQ는 없다.

## 새 환경 준비

서로 다른 DB로 연결되는 구성을 기동 전에 거부한다. `AI_DATABASE_NAME=emmet4930_<전용이름>` 한 개를 사용한다. Compose DSN 호스트는 모두 `postgres:5432`다. 서비스별 계정은 `ai_chat_service`, `ai_profiler_service`, `ai_search_service`, `ai_catalog_sync`, 마이그레이션 계정은 `ai_migrator`다. 앱 계정은 superuser/CREATEDB/CREATEROLE를 가지면 안 된다. 운영용 계정 생성·키 설정·기존 역할 변경은 이 후보가 자동 수행하지 않는다. 환경 관리자가 새 DB·역할·pgvector 확장·인증을 준비한다. `ai_migrator`는 그 DB의 스키마 소유권과 제한 grant를 적용할 권한이 필요하다.

모든 앱의 부팅 DDL을 명시적 마이그레이션으로 옮겼다. 새 DB 또는 **별도 복사한 후보 DB**에서만 다음을 수행한다. 기존 실행 DB를 되감거나 `0007`을 재실행하지 않는다. 이미 `0008` 이상인 DB에는 수정된 `0007`이 소급 적용되지 않는다.

```sh
python -m integration.migrate_single_server --database "$AI_DATABASE_NAME" --emmet
```

`MIGRATION_DATABASE_URL_FILE`은 기존에 준비한 마이그레이션 DSN 파일 경로다. `PROFILING_APP_DB_PASSWORD`가 있으면 역할 자격 증명 변경을 피하기 위해 거부한다. 이 명령은 Emmet `0011`까지 적용하고 통합 스키마·소유권을 적용한다. 같은 DB·버전의 재실행은 변경 없이 끝난다. 이번 검증은 새 임시 DB의 fresh 경로와 0010→0011 경로에서 기존 개인 선호·비선호 및 이전 앱 소유 스키마를 보존하는지 확인했다.

## 상품 세대와 동기화

BE product export에는 다음 완전성 증거가 필요하다. 실제 BE와 합의·연결되지 않았으므로 현재는 합성 계약 서버만 검증했다.

```json
{"data":{"generatedAt":"2026-10-16T02:00:00Z","products":[],"snapshot":{"id":"<UUID>","complete":true,"productCount":0,"sha256":"<canonical products+categories SHA256>"}}}
```

정확한 digest 구현은 `product-search/search/complete_export.py`다. 카테고리 export와 상품 export가 동일 snapshot을 나타내야 한다. count/digest 불일치·부분 응답·이전 시각·같은 ID 다른 내용은 거부한다. 단순 누락을 삭제로 추정하지 않는다. 완전성 증거가 확인되면 사라진 ID의 tombstone을 먼저 저장한다. 이후 임베딩 실패나 rollback이 있어도 삭제된 ID가 과거 세대/콜백에서 재등장하지 않는다. 따라서 실패 시 예전 세대로 계속 검색할 수 있지만 확인된 삭제는 즉시 제외된다. 삭제 ID의 복원은 별도 BE 복원 계약이 필요하며 자동 해제하지 않는다.

순서는 `BE fetch → 완전성 검증 → immutable PG generation → 캐시·인덱스 검증 → pending → 실제 Search 검증 → 짧은 active pointer commit → 메모리 교체`다. 동기화 job은 활성 포인터를 바꿀 수 없다. fetch/embedding/validation 실패는 기존 활성 세대를 유지한다. Search가 죽은 경우 pending을 남기고 재시작한 Search가 재검증한다. PG commit 직후 프로세스가 죽어도 다음 시작에서 PG 활성 세대를 복구한다. PG를 확인할 수 없으면 보유 스냅샷 요청도 503으로 실패시킨다. 확인되지 않은 삭제 데이터를 계속 제공하지 않기 위해서다.

동기화 실행은 같은 Search 이미지의 별도 일회성 프로세스다. 두 job은 PG advisory lock으로 직렬화된다. Search도 DB lease로 단일 프로세스를 강제한다. 여러 worker/replica를 실행하지 않는다. 검색과 sync는 서로 다른 캐시 볼륨을 사용한다. 파일만 삭제돼도 PG로부터 다시 구성한다.

```sh
# 기본은 dry run. 명시적 --apply가 있어야 pending 세대를 제안한다.
python -m integration.search_sync --export-url "$BACKEND_EXPORT_URL" --categories-url "$BACKEND_CATEGORIES_URL"
python -m integration.search_sync --export-url "$BACKEND_EXPORT_URL" --categories-url "$BACKEND_CATEGORIES_URL" --apply
```

`CATALOG_SYNC_DATABASE_URL_FILE`, `BACKEND_SERVICE_TOKEN_FILE`, `AI_DATABASE_NAME`, `SEARCH_RUNTIME_ROOT`, `ASSET_ROOT`, `ENCODER_CODE_ROOT`을 지정한다. URL은 같은 origin, HTTPS를 기본으로 하고 인증 redirect를 따르지 않는다. loopback 이외 사설 HTTP는 `BACKEND_EXPORT_ALLOW_PRIVATE_HTTP=1`을 명시해야 한다. 실제 인증값이나 DSN을 명령행에 쓰지 않는다.

## 공용 API와 모델 계약

내부 Bearer 인증을 사용한다. `/v1/catalog/contract`, `/v1/catalog` 전체 상품 페이지, `/v1/catalog/bindings` 보유 세대 lease, `/v1/embeddings/query`, `/v1/embeddings/products`, `/v1/vector-search`, 기존 `/v1/search`, `/v1/products`, `/v1/metadata`를 제공한다. Profiler는 전체 상품·벡터를 읽어 기존 정책과 MMR을 유지한다. 순수 벡터 검색을 hybrid 점수로 대체하지 않았다.

계약은 `snapshotId`, `catalogVersion`, `visibilityRevision`, 모델·revision·weight SHA, encoder fingerprint, dimensions, 전처리, 거리, 태그 규칙·해시를 제공한다. 현재 encoder는 `jinaai/jina-embeddings-v5-text-nano-retrieval`, revision `ac5d898c8d382b17167c33e5c8af644a3519b47d`, 768차원, q4f16, 1024 token, Query/Document prefix, L2 정규화·cosine이다. fingerprint는 lock·query service·runtime·document code·실제 모델 파일을 포함한다. 서로 다른 vector space를 섞지 않으며 모델 변경은 명시적 drain/restart 및 새 세대 준비 작업이다. hot swap으로 처리하지 않는다.

## 프로필과 외부 BE 계약

7.6 요청·7.7 콜백, Chat HTTP/SSE 및 `productId/reason` 계약은 유지한다. Profiler의 30개 후보와 Chat의 최대 3개 표시는 별개다. 같은 sourceVersion은 저장된 콜백 본문을 재사용하며, 보내기 직전 현재 상품 가시성을 확인한다. 삭제된 상품이 포함되면 재분석·ID 수정·콜백 전송 없이 기존 기록을 보존한다. 이미 BE가 받은 과거 추천을 원격에서 회수하는 기능은 없다.

`POST /internal/v1/profiles/read`는 service token 외에 서버 발행 `grantId`, 정확한 caller·recipient·sourceVersion·만료·미철회 여부를 검증한다. 선호/비선호 태그만 반환하고 원문·리뷰·추천 pool을 보내지 않는다. Chat 어댑터는 명시적 BE 문맥의 null/빈 목록도 존중하고 빠진 필드만 보충한다. 401/403은 fallback하지 않으며, 만료/실패 시 유효한 명시적 BE 문맥만 fallback에 쓴다. 현재 BE에 grant 발행·철회·신원 전달 계약이 없어 **Chat의 기존 외부 요청에 자동 연결하지 않았다**. 데모 권한이나 추정된 recipient ID로 이 공백을 메우지 않는다.

BE400의 하루 10턴·취향 수정 3크레딧은 BE 소유의 미구현 계획이다. AI에 새 할당량·과금 처리를 추가하지 않고 기존 예산 예약·멱등성·소유권을 보존한다.

## 컨테이너 실행 순서

`integration/containers/compose.guarded.yaml`은 Chat·Profiler·Search·PG와 내부 mesh, 별도 migration profile이다. 실제 BE callback origin·기존 비용 gateway·이미 준비된 secret 파일이 필요하다. 합성 fake BE/Console은 `compose.smoke.yaml`에만 있다. Chat·Profiler는 loopback 호스트 포트, Search는 내부에서만 열린다. public ingress를 추가하지 않았다.

이전 후보의 Apple Linux/arm64 이미지 시험은 당시 소스에 대한 과거 증거다. 이번 최종 tree의 Linux 이미지·전체 Compose/DNS·linux/amd64·guarded production target 종단 실행은 미검증이다. 이번에는 새 임시 PG18과 기존 CPU 자산을 사용한 무료 native 검증만 수행했다. 아래 명령은 별도 승인된 Linux/Compose 환경에서 검증할 절차이며 이번 작업에서 실행하지 않았다.

```sh
docker compose -f integration/containers/compose.guarded.yaml -f integration/containers/compose.sync.yaml config --quiet
docker compose -f integration/containers/compose.guarded.yaml build search profiler chat migrate
docker compose -f integration/containers/compose.guarded.yaml up -d postgres mesh
docker compose -f integration/containers/compose.guarded.yaml --profile migration run --rm migrate
docker compose -f integration/containers/compose.guarded.yaml -f integration/containers/compose.sync.yaml --profile sync run --rm catalog-sync
docker compose -f integration/containers/compose.guarded.yaml up -d search profiler chat
```

초기 sync는 pending을 만든다. 첫 Search 부팅이 이를 검증·활성화한다. 이후 같은 sync 명령을 외부 스케줄러에서 일회성으로 실행할 수 있다. 스케줄러·배포·RabbitMQ는 이번 후보에 만들지 않았다. PostgreSQL 볼륨은 사전에 준비된 후보 전용 external volume이어야 한다. Chat의 Python 3.12 base는 동결 후보의 이미지 digest를 유지한다. Search 재시작은 PostgreSQL에서 동일 catalog/snapshot을 복구한다. Profiler가 갖고 있던 메모리 binding은 사라지므로 503으로 차단하고, Profiler를 명시적으로 재시작해 새 binding을 받는다. 자동 재바인딩을 구현한 것으로 간주하지 않는다.

## 재현 가능한 검증

기존 설치된 서비스별 `.venv`를 read-only로 사용한다. `ENVIRONMENTS`는 Chat Python 3.12 / Profiler·Search Python 3.13의 환경 루트, `ENCODER_ASSETS`는 검증된 `models/`, `node_modules/`를 가진 encoder 디렉터리다. 모델 다운로드·유료 API 호출 없이 실행할 수 있다.

```sh
python3 -B integration/tests/run_upgrade_offline.py --environment-root "$ENVIRONMENTS" --output "$NEW_OUTPUT/offline"
"$ENVIRONMENTS/profiling/.venv/bin/python" -B integration/tests/run_single_server_tests.py \
  --pg-bin "$PG_BIN" --output "$NEW_OUTPUT/native" \
  --dependency-path "$ENVIRONMENTS/product-search/.venv/lib/python3.13/site-packages" \
  --dependency-path "$ENVIRONMENTS/chat/.venv/lib/python3.12/site-packages" \
  --environment-root "$ENVIRONMENTS" --encoder-assets "$ENCODER_ASSETS"
```

두 번째 명령은 새 loopback 임시 PG만 만든다. fresh·upgrade, 제한 SQL 역할, 실패·PG 재시작·uncertain commit, 삭제·rollback, 빈 완전 export, 실제 CPU encoder, 세 서비스 HTTP·SSE·중복·재시작을 검증하고 종료한다. 예전 실행 DB의 위치를 받지 않는다. `commands.jsonl`과 JUnit을 남긴다.

실제 LLM은 별도 승인을 받은 opt-in `--live`에서만 실행한다. 기존 연결·누적 사용·미확정 예약과 gateway 계약을 먼저 확인해야 한다. 무료 회귀는 해당 경로를 호출하지 않는다. 기존 원장을 초기화·복사하거나 새 계정으로 한도를 우회하지 않는다. 이번 후보에서는 비용 원장과 실제 provider를 사용하지 않았다.

향후 승인된 실제 모델 검증에도 합성 사례만 사용하고 가격·개수·비선호·검색 근거의 정성 검토를 HTTP 성공 및 자동 검사와 구분해야 한다. 이전 후보의 소수 사례 성공을 현재 tree의 품질 평가로 합산하지 않는다.

제외 상태 수명은 무료 `chat/tests/test_exclusion_lifecycle.py`로 검증한다. 별도 유료 실행 선택지인 `--live --live-case exclusion`은 이번 작업에서 실행하지 않았다.

## 추가 검토: 제외 출처·전환·저장 태그

`lowLatencyState.productExclusionSources`는 개별 상품이 지속 제외 목록에 추가된 턴과 공개 발언을 보존한다. 목록 교체 시 유지된 항목은 원래 출처를 유지하고, 제거된 항목만 지운다. 예전 `filterSources.exclude_product_ids`는 마지막 목록 변경 기록일 뿐 모든 항목의 출처가 아니다. 새 항목별 정보가 없는 과거 상태는 출처 미상으로 전달하며 임의로 해제하지 않는다. 이 수정 후 추가 유료 검증은 하지 않았다. 이전 8턴 실측은 이전 소스에 대한 증거이며 현재 혼합 출처 수정의 모델 품질 보증이 아니다.

같은 인코더의 상품 세대 publish는 Profiler의 retained binding을 유지한다. Chat은 최초 snapshot을 후속 검색·상품 조회에 전달하고 세대 불일치를 실패로 처리한다. Profiler refresh는 접수를 닫고 수락된 큐·실행을 비운 뒤 DB 실행 기록도 확인한다. `RUNNING/RESULT_READY/FAILED`가 하나라도 남으면 기존 바인딩을 유지하며 다른 세대로의 전환을 보류한다. 동일 snapshot·전체 contract의 만료 lease 재획득은 기존 작업 복구를 위해 허용한다. 새 수신자 요청·콜백 재전송으로 해소할 수 있게 실패한 refresh 뒤 기존 접수는 재개한다. `DELIVERED/SUPERSEDED` 기록은 보존한다.

인코더 변경은 hot swap 대상이 아니다. 운영 전환의 필요한 순서는 **Chat·Profiler 신규 접수 닫기 → 이미 수락한 구세대 작업 완료 → 새 claim/재접수 중단 → 두 서비스의 미완료·재개 가능 구세대 작업이 없음 확인 → Search/인코더 전환 → Profiler 재바인딩 → 검증 후 접수 재개**다. Chat의 operator-only `Store.pause_admissions(True)`는 실행 중인 턴이 있어도 먼저 새 접수를 닫고, 기존 턴의 완료·멱등 재조회는 허용한다. `ai_chat.turns`의 PROCESSING 0개와 runtime lease/worker 종료, Profiler 큐 0개와 위 DB 실행 기록 0개를 함께 확인해야 한다. 기존 run을 강제 완료/삭제해서 검사를 통과시키지 않는다.

전체 순서를 자동 실행하거나 Search publish를 두 서비스의 단일 전환 gate에 연결한 조정기는 **구현하지 않았다**. 현재 자동 상품 publish의 안전 근거는 같은 인코더 + 바인딩/snapshot 검사다. 두 소비자의 무중단 성공이나 자동 encoder 전환을 보장하지 않는다. 관리자가 승인한 maintenance 절차로 양쪽 접수/claim을 닫았다는 증거가 없으면 encoder 전환을 진행하면 안 된다. 이 후보에서는 실제 전환·배포를 실행하지 않았다.

저장 프로필에는 추론 당시 vocabulary hash가 없으므로 `POST /internal/v1/profiles/read`는 `catalogVersion`과 `tagTaxonomy:null`을 반환한다. opt-in Chat 어댑터는 현재 공용 catalog contract와 저장된 taxonomy/validator 증거가 모두 일치할 때만 추론 태그를 보충하도록 제한한다. 현재 서버는 해당 증거를 만들지 않으므로 저장 선호 태그 자동 보충은 비활성이고, 미검증 비선호 태그를 조용히 버리는 대신 503으로 차단한다. 현재 BE의 명시적 값·null·[] 우선순위는 유지한다. 원래 외부 Turn API에는 이 reader를 아직 연결하지 않았다.

Search contract의 `categoryTaxonomy`는 상품이 없는 분류도 포함한다. Profiler는 매핑 불가능한 명시적 비선호 분류를 모델 호출 전에 거부한다. 해석 가능한 태그의 기존 어휘·순위 정책과 사전 계산 상품 태그 hash는 바꾸지 않았다. 이번 회귀·재현 증거는 새 후보와 함께 제공하는 `verification/`와 파일 manifest에 기록한다.
