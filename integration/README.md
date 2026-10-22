# AI 통합 실행

Search·Profiler·Chat은 같은 PostgreSQL18/pgvector와 Search 상품 카탈로그를 사용합니다. 별도 Budget Gateway·NUC·비용 원장·예약·정산 서비스는 사용하지 않습니다. OpenRouter 키의 크레딧 한도는 제공자에서 관리하며 앱이 그 한도 설정을 자동 검증한다고 가정하지 않습니다. 기존 요청 종료·동시성·재시도·출력 길이 제어는 유지합니다.

저장 취향은 기존 프로파일링 입력으로 AI DB에 보관하고 인증된 BE 대화의 고정 수신자·현재 분석 버전을 확인해 모델 문맥에 전달합니다. 새 BE grant API는 필요하지 않으며, 분석 대기·실패·버전 불일치는 명시합니다. 변경된 조회 함수는 기존 AI 마이그레이션으로 적용합니다.

## 기본 실행

저장소 루트에서 **`integration/containers/compose.yaml` 하나**를 사용합니다. Console과 Catalog Sync는 선택 도구이며 기본 앱 기동에 필요하지 않습니다. 기존 자격증명은 플랫폼 환경변수 또는 운영자가 이미 준비한 Secret으로 주입합니다. `.env`의 항목 전체가 컨테이너에 자동 전달되는 것은 아니며 이 Compose는 필요한 항목을 `environment`에 명시합니다. `docker compose config` 전체나 환경 전체를 출력하지 마세요.

| 설정 | 용도 |
|---|---|
| `AI_DATABASE_NAME` | `ai_db_develop`, `ai_db_staging`, `ai_db_production` 중 명시한 DB. 모든 DSN과 일치해야 함 |
| `AI_DATABASE_HOST` | 로컬 기본 `postgres`, ECS에서는 실제 RDS 호스트 |
| `SEARCH_CATALOG_DATABASE_URL` | 같은 DB의 `ai_search_service` DSN |
| `PROFILING_DATABASE_URL` | 같은 DB의 `ai_profiler_service` DSN |
| `DATABASE_URL` | 같은 DB의 `ai_chat_service` DSN |
| `AI_INTERNAL_SERVICE_TOKEN` | Search 등 AI 내부 통신 인증 |
| `BE_SERVICE_TOKEN` | Chat·Profiler가 공유하는 기존 BE 서비스 인증 |
| `OPENROUTER_API_KEY_CHAT`, `OPENROUTER_API_KEY_PROFILER` | 역할별 기존 OpenRouter 키 |
| `SESSION_SECRET`, `QA_ADMIN_PASSWORD`, `DEMO_ACCESS_TOKEN` | 기존 Chat 세션·관리·체험 경로 인증 |
| `PROFILING_BACKEND_BASE_URL` | 실제 BE 콜백 origin |
| `AI_ENVIRONMENT` | `local`, `develop`, `production`만 허용. 환경 이름이 staging이면 이 설정에는 `develop` 사용 |
| `EMMET_MODEL_DIR`, `EMMET_MEDIA_DIR` | 기존 검증된 모델·상품 이미지 폴더 |
| `EMMET_POSTGRES_VOLUME` | 로컬 Compose에서 사용할 별도 준비된 PG18 볼륨 |
| `MIGRATION_DATABASE_URL` | 명시적 one-off 작업에서만 쓰는 `ai_migrator` DSN |

기본 Compose는 준비된 로컬 DB 볼륨을 재사용합니다. 운영 데이터 볼륨으로 시험하지 마세요. DB·역할·확장·마이그레이션·초기 카탈로그 준비는 앱 기동과 분리합니다. 앱이 계정을 만들거나 권한을 확대하지 않습니다. 기존 DB 백업과 복구는 [RECOVERY.md](RECOVERY.md)를 따릅니다.

```sh
docker compose -p seonjalal-ai -f integration/containers/compose.yaml config --quiet
docker compose -p seonjalal-ai -f integration/containers/compose.yaml up --build --wait
```

ECS에서는 Compose나 gateway 없이 공식 `search`, `profiler`, `chat` 이미지 target과 역할 command를 실행합니다. Secrets Manager의 기존 값은 ECS `secrets`로 위 역할별 환경변수 이름에 주입할 수 있습니다. 환경변수 값이 있고 과거 로컬 `*_FILE` 경로가 존재하지 않으면 주입값을 사용합니다. 실제 파일이 존재하면 기존 파일 우선 규칙을 유지하며 다른 이름의 키·인증 출처 충돌은 계속 거부합니다. `OPENROUTER_MANAGED_KEY_DIR`·`BE_SERVICE_TOKEN_DIR`는 선택 Console 파일 저장 모드 전용이므로 env-only ECS에는 임의로 설정하지 마세요.

Migration과 Catalog Sync는 별도 one-off입니다. `--emmet` 마이그레이션은 Alembic 실행 전에도 대상 DB 이름·호스트·역할을 검증합니다. Catalog Sync는 `compose.sync.yaml`의 명시적 `sync` 프로필 또는 동일 Search 이미지의 `integration.search_sync` 명령을 사용합니다. 모델·카탈로그 번들과 활성 세대의 hash/분류 일치 검증을 유지합니다. 모델 준비는 [자산 설치](../console/ASSET_SETUP.md), 상품 적재는 [export 계약](BE_EXPORT_CONTRACT_DRAFT.md)을 참고하세요.

이 변경은 기존 BE의 대화 생성·`message` 요청 계약을 그대로 유지합니다. 저장 프로필 개인화 연결은 별도 변경이며 새 BE API·grant·마이그레이션을 요구하지 않습니다.

## 무료 합성 검증

합성 Compose는 새 프로젝트와 새 볼륨에 모의 BE·모의 모델을 띄우며 gateway나 가짜 비용 원장은 없습니다. 기존 CPU 모델 자산을 읽기 재사용합니다.

```sh
export EMMET_MODEL_DIR=/absolute/path/to/verified/models
docker compose -p ai-synthetic-new -f integration/containers/compose.smoke.yaml --profile check build
docker compose -p ai-synthetic-new -f integration/containers/compose.smoke.yaml up --wait
docker compose -p ai-synthetic-new -f integration/containers/compose.smoke.yaml --profile check run --rm --no-deps smoke-check
docker compose -p ai-synthetic-new -f integration/containers/compose.smoke.yaml down
```

이 경로의 모델은 mock이며 실모델·실BE·AWS 성공 증거가 아닙니다. 무료 회귀는 `integration/tests/run_upgrade_offline.py`, 격리 PG/CPU 검증은 `run_single_server_tests.py`를 사용합니다. 실제 모델 검증은 합성 입력과 별도 테스트 하네스의 승인된 누적 비용 한도 안에서 수행합니다. 테스트용 호출 수 제어는 앱의 별도 비용 원장이나 일반 서비스 승인 정책이 아닙니다.
