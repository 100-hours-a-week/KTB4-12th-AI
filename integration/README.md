# 통합 실행

Chat은 대화와 상품 카드, Profiler는 취향·비선호·재랭킹과 개인 프로필, Search는 상품·임베딩·검색을 맡습니다. Chat과 Profiler는 Search API를 사용하고, BE 상품 동기화는 Search와 같은 코드의 별도 작업으로 실행합니다. RabbitMQ는 사용하지 않습니다.

DB는 PostgreSQL 18 하나를 사용합니다. Chat은 `ai_chat`, Profiler는 `ai_profile`, Search와 동기화 작업은 `ai_catalog`에 접근합니다. 상품 파일과 메모리 인덱스는 DB에서 복구할 수 있는 캐시입니다. 에멧 스키마에 필요한 pgvector 확장은 유지하지만 검색 알고리즘을 pgvector로 바꾸지는 않았습니다.

Profiler가 받은 수신자 입력과 현재 버전의 분석은 AI DB에 보관합니다. Chat의 개인화에는 대화에 묶인 발신자·수신자·입력 버전이 맞는 만료되지 않은 조회 권한이 필요합니다. 권한이 없거나 철회·만료되면 비공개 취향을 사용하지 않고 일반 대화를 이어갑니다. 분석이 아직 끝나지 않은 원문은 긍정 검색어로 쓰지 않습니다. 권한은 이 앱이 임의 발급하지 않으며 실제 BE의 발급·철회 연결은 별도 확인이 필요합니다. 기존 `/1` DB도 배포 전 아래 `migrate` 명령으로 입력 테이블·제한된 조회 함수를 추가하세요.

## 합성 데이터로 실행

저장소 루트에서 실행합니다. Docker Engine·Compose와 [model.lock.json](../product-search/embedding/model.lock.json)에 맞는 모델 파일이 필요합니다. `EMMET_MODEL_DIR`는 `models` 폴더 자체의 절대 경로입니다. 모델 준비는 [자산 설치](../console/ASSET_SETUP.md)를 참고하세요.

```sh
export EMMET_MODEL_DIR=/absolute/path/to/verified/models
docker compose -p ai-rc-synthetic -f integration/containers/compose.smoke.yaml config --quiet
docker compose -p ai-rc-synthetic -f integration/containers/compose.smoke.yaml --profile check build
docker compose -p ai-rc-synthetic -f integration/containers/compose.smoke.yaml up --wait --wait-timeout 180
curl --fail http://127.0.0.1:18811/healthz
docker compose -p ai-rc-synthetic -f integration/containers/compose.smoke.yaml --profile check run --rm --no-deps smoke-check
docker compose -p ai-rc-synthetic -f integration/containers/compose.smoke.yaml down
```

Console은 `http://127.0.0.1:18811`이며 로그인이 필요합니다. 호스트에서 위 `curl`이 성공해야 공개 포트까지 확인한 것입니다. Console만 호스트 loopback에 공개하고 AI 서비스·DB는 외부 경로 없는 내부 네트워크에 둡니다. 합성 시험 비밀번호는 Console 컨테이너의 `/run/fixture/console_admin_password`에 있습니다. 합성 상품·토큰과 모의 BE·모델 응답을 사용하므로 실제 키가 필요하지 않습니다. `down`은 DB 볼륨을 남깁니다. 이 명령은 **새 프로젝트와 빈 합성 볼륨의 최초 실행용**입니다. 이미 초기화한 볼륨에서는 `qa-init`이 재실행을 거부하므로 [복구 안내](RECOVERY.md#합성-compose-복구)를 따르세요. 새 시험은 새 프로젝트명으로 시작하고, 이전 합성 볼륨 삭제는 해당 프로젝트의 데이터인지 확인한 뒤 진행합니다.

검증 범위: `cf6ef26`의 Linux/arm64 이미지·합성 Compose 기동·복구와 fresh/0010→0011 마이그레이션을 확인했습니다. 별도 native 환경에서 실제 모델로 합성 취향·추천 1건씩의 조건 충족도 확인했습니다. 실제 BE export·콜백·grant, 운영 설정 전체 기동, Linux/amd64와 다양한 입력의 추천 품질은 미검증입니다.

## 실제 서비스 준비

기본 운영은 `compose.guarded.yaml`에 `compose.admin.yaml`과 `compose.sync.yaml`을 병합합니다. admin 설정의 기본 `provider-key-limits` 모드는 Chat·Profiler의 OpenRouter 키 한도를 각각 사용하며 자체 비용 원장·예산 gateway를 요구하지 않습니다. [Console 안내](../console/README.md)에 따라 관리자 로그인·대상 설정을 준비하고 화면에서 키 두 개와 공통 BE 토큰을 입력합니다. 기존 `guarded` 환경은 admin 설정 없이 명시적 `guarded` 설정으로 기존 원장·예약을 보존해 계속 사용할 수 있습니다. 아래 값을 준비하고, 기존 DB를 옮길 경우 먼저 [백업·마이그레이션 절차](RECOVERY.md)를 확인하세요. `_FILE`에는 비밀값이 든 외부 파일의 경로를 넣습니다. 비밀값이나 DSN을 명령행·Git에 넣지 않습니다.

| 설정 | 준비할 값 |
|---|---|
| `AI_DATABASE_NAME`, `EMMET_POSTGRES_VOLUME` | 전용 `emmet4930_...` DB 이름과 준비된 PG18 external volume |
| `CHAT_DATABASE_URL_FILE`, `PROFILER_DATABASE_URL_FILE`, `SEARCH_DATABASE_URL_FILE` | 같은 `postgres:5432` DB의 서비스별 DSN 파일 |
| `MIGRATION_DATABASE_URL_FILE`, `CATALOG_SYNC_DATABASE_URL_FILE` | `ai_migrator`, `ai_catalog_sync`의 DSN 파일 |
| `AI_SERVICE_TOKEN_FILE`, `SESSION_SECRET_FILE`, `QA_ADMIN_PASSWORD_FILE`, `DEMO_ACCESS_TOKEN_FILE` | 서비스·세션·관리자·체험 인증 파일 |
| `STORE_TOKEN_FILE`, `CHAT_OPENROUTER_KEY_FILE`, `PROFILER_OPENROUTER_KEY_FILE` | 기존 guarded 모드에서만 필요한 원장 토큰과 모델 키 파일 |
| `BUDGET_GUARDED_ORIGIN`, `BUDGET_EXPECTED_MAX_ATTEMPTS`, `BUDGET_EXPECTED_MAX_RESERVED_USD` | 기존 guarded 모드에서만 필요한 예산 gateway와 호출·예약 한도 |
| `PROFILING_BACKEND_BASE_URL` | BE 프로필 콜백 origin |
| `BACKEND_EXPORT_URL`, `BACKEND_CATEGORIES_URL`, `BACKEND_EXPORT_TOKEN_FILE` | [export 계약](BE_EXPORT_CONTRACT_DRAFT.md)을 만족하는 같은 origin의 URL과 인증 파일 |
| `EMMET_MODEL_DIR`, `EMMET_MEDIA_DIR` | 읽기 전용 모델·상품 이미지 폴더 |
| `INTEGRATION_CHAT_PORT`, `INTEGRATION_PROFILER_PORT` | 선택 설정. 기본 loopback 포트는 각각 18860, 18861 |

앱 역할은 `ai_chat_service`, `ai_profiler_service`, `ai_search_service`입니다. DB·역할·pgvector 확장·인증은 미리 준비하며 앱에 superuser/CREATEDB/CREATEROLE를 주지 않습니다. `ai_migrator`는 해당 DB의 스키마 소유권과 필요한 권한을 적용할 수 있어야 합니다. 앱 부팅은 DDL이나 계정 생성을 하지 않습니다.

실제 BE export가 준비되기 전에는 아래 동기화 단계로 진행할 수 없습니다. 준비가 끝난 새 환경의 순서는 다음과 같습니다.

```sh
docker compose -f integration/containers/compose.guarded.yaml -f integration/containers/compose.admin.yaml -f integration/containers/compose.sync.yaml config --quiet
docker compose -f integration/containers/compose.guarded.yaml -f integration/containers/compose.admin.yaml -f integration/containers/compose.sync.yaml --profile migration --profile sync build
docker compose -f integration/containers/compose.guarded.yaml -f integration/containers/compose.admin.yaml up -d postgres mesh
docker compose -f integration/containers/compose.guarded.yaml -f integration/containers/compose.admin.yaml --profile migration run --rm migrate
docker compose -f integration/containers/compose.guarded.yaml -f integration/containers/compose.admin.yaml -f integration/containers/compose.sync.yaml --profile sync run --rm catalog-sync
docker compose -f integration/containers/compose.guarded.yaml -f integration/containers/compose.admin.yaml up --wait --wait-timeout 180 search
docker compose -f integration/containers/compose.guarded.yaml -f integration/containers/compose.admin.yaml up -d console
# Console에 로그인하여 키·BE 토큰을 등록하거나 기존 주입값을 확인한 뒤 실행합니다.
docker compose -f integration/containers/compose.guarded.yaml -f integration/containers/compose.admin.yaml up --wait --wait-timeout 180 profiler chat
```

키가 없는 Chat은 실행 중이어도 `/readyz`가 503을 반환하므로 `up --wait`가 완료되지 않을 수 있습니다. Console은 Chat의 준비 상태에 의존하지 않아 먼저 로그인·설정할 수 있습니다. 관리자 해시가 없는 Console의 보호 API는 열리지 않습니다.

이후 상품 갱신에도 같은 `catalog-sync` 명령을 사용합니다. 작업은 새 세대를 DB에 준비하고, 실행 중인 Search가 검증한 뒤 활성화합니다. 동기화 작업은 검색 요청을 받지 않으며 별도 캐시를 사용합니다. Search는 한 프로세스로 실행합니다. Search 포트는 내부에만 있고 Chat·Profiler만 loopback에 열립니다. 외부 공개·TLS 설정은 별도로 준비해야 합니다.

관리 화면은 위 `compose.admin.yaml` 조합에 포함됩니다. 비공개 대상 설정·관리자 해시와 HTTPS 프록시는 [Console 안내](../console/README.md)에 따라 준비합니다. 관리 화면은 Chat·Profiler 키 두 개와 공통 BE 토큰을 영속 파일에 저장합니다. `AI_ENVIRONMENT`는 세 프로세스에 같은 환경으로 지정하고, 비용 정책은 기본 `provider-key-limits`를 사용합니다. Console 대상 파일에 `budgetMode`를 명시했다면 실행기와 같아야 합니다. 이 조합은 모델 키 파일·예산 gateway 없이 시작하며 화면에서 키를 입력합니다. 기존 DB·Search·내부 인증·관리자 로그인 준비는 여전히 필요합니다. 개발·운영은 서로 다른 Compose 프로젝트/볼륨을 사용하세요. Develop의 단일 팀 Secret·일반 환경변수 연결과 대시보드 저장 우선순위는 [설정 등록](../console/README.md#develop-설정-등록)을 따릅니다. 앱에서 AWS Secret을 쓰지는 않습니다. 기존 `/1` DB도 배포 전에 위 `migrate` 명령으로 관리자 평가 테이블을 추가합니다.

## 연동할 때

- [Chat HTTP/SSE](../chat/INTEGRATION.md): `productId/reason` 카드 최대 3개, 더 적게 요청한 수를 존중합니다. 무료 연동은 [고정 응답 테스트](../chat/TESTING.md)를 사용하세요.
- [Profiler 요청·콜백](../profiling/README.md): 개인 프로필과 후보 최대 30개는 Profiler가 관리합니다. 같은 수신자·`sourceVersion`·본문의 저장된 완료 결과는 콜백을 재사용합니다. 전송 전에 삭제 상품을 검사합니다.
- [Search 기본 API](../product-search/INTEGRATION.md): 공용 조회에는 `/v1/catalog/contract`, `/v1/catalog`, `/v1/catalog/bindings`, `/v1/embeddings/query`, `/v1/embeddings/products`, `/v1/vector-search`도 제공합니다. Bearer 인증과 같은 `snapshotId`를 사용하세요.

벡터 계약은 Jina v5 nano retrieval, lock에 고정된 revision, q4f16·768차원·1024 token·`Query: `/`Document: ` 접두어·L2 정규화·cosine입니다. `/v1/catalog/contract`의 모델·encoder fingerprint·카탈로그·태그 버전을 함께 확인해야 하며 모델 이름과 차원만 같다고 벡터를 섞으면 안 됩니다.

개인 프로필 읽기 `POST /internal/v1/profiles/read`에는 service token 외에 BE가 발행한 `grantId`, caller·recipient·sourceVersion, 만료·철회 검사가 필요합니다. 원문·리뷰·추천 pool은 반환하지 않습니다. 401/403은 대체 처리하지 않으며, 조회 불가 시에는 유효한 명시적 BE 문맥만 사용합니다. 현재 BE grant 발행과 외부 Chat 요청 연결은 미완료입니다. 저장 프로필의 `tagTaxonomy:null`을 현재 태그 판으로 간주하지 않으며, 미검증 비선호 태그는 503으로 차단합니다. BE가 명시한 값·`null`·`[]`를 우선합니다. 사용자별 일일 횟수·크레딧은 기존 실험 비용 원장과 별개의 BE 정책입니다.

## 무료 회귀

로컬 검사는 Python 3.12(Chat), 3.13(Profiler/Search), Node 24 이상과 uv를 사용합니다. 설치에는 다운로드가 필요할 수 있지만 아래 회귀 실행은 외부 HTTP/DB를 차단합니다.

```sh
uv sync --project chat --frozen --group test --python 3.12
uv sync --project profiling --frozen --python 3.13
uv sync --project product-search --frozen --python 3.13
python3 -B integration/tests/run_upgrade_offline.py --environment-root "$PWD" --output /tmp/ai-rc-check
```

출력 경로는 매번 새로 지정합니다. DB·실제 CPU encoder 검사는 기존 환경 루트 `ENVIRONMENTS`, PostgreSQL 도구 폴더 `PG_BIN`, `models/`와 `node_modules/`가 든 `ENCODER_ASSETS`, 새 결과 폴더 `NEW_OUTPUT`을 준비한 뒤 실행합니다. 이 실행기는 새 loopback 시험 DB만 만들고 끝나면 종료합니다.

```sh
"$ENVIRONMENTS/profiling/.venv/bin/python" -B integration/tests/run_single_server_tests.py \
  --pg-bin "$PG_BIN" --output "$NEW_OUTPUT/native" \
  --dependency-path "$ENVIRONMENTS/product-search/.venv/lib/python3.13/site-packages" \
  --dependency-path "$ENVIRONMENTS/chat/.venv/lib/python3.12/site-packages" \
  --environment-root "$ENVIRONMENTS" --encoder-assets "$ENCODER_ASSETS"
```
