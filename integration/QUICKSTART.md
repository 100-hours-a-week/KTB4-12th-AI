# 팀 실행 quickstart

저장소 루트에서 실행합니다. 기본 선택은 유료 호출 없는 합성 검증입니다. 모델은 기존 검증본을 읽기 전용으로 연결합니다. 실제 상품·대화·환경·모델 파일은 저장소 밖에 둡니다.

## 무료 회귀

Python 3.12(Chat), 3.13(Profiler/Search), Node 24 이상과 uv가 필요합니다. lock 변경 없이 환경을 준비합니다. 설치 단계는 패키지 다운로드가 필요할 수 있습니다.

```sh
uv sync --project chat --frozen --group test --python 3.12
uv sync --project profiling --frozen --python 3.13
uv sync --project product-search --frozen --python 3.13
python3 -B integration/tests/run_upgrade_offline.py --environment-root "$PWD" --output /tmp/ai-rc-check
```

출력은 매번 새 경로를 사용합니다. 설치된 환경은 `--environment-root`로 재사용합니다. 시험은 외부 HTTP/DB를 차단하며 모델 다운로드·LLM 호출을 하지 않습니다. PostgreSQL fresh/upgrade·CPU encoder 명령은 [운영 가이드](SINGLE_SERVER_GUIDE.md#재현-가능한-검증)에 있습니다.

## Linux 합성 Compose

Docker Engine + Compose v2, 충분한 공간, model.lock.json과 일치하는 기존 모델이 필요합니다. `EMMET_MODEL_DIR`는 `models` 디렉터리 자체의 절대 경로입니다.

```sh
export EMMET_MODEL_DIR=/absolute/path/to/verified/models
docker compose -p ai-rc-synthetic -f integration/containers/compose.smoke.yaml config --quiet
docker compose -p ai-rc-synthetic -f integration/containers/compose.smoke.yaml build
docker compose -p ai-rc-synthetic -f integration/containers/compose.smoke.yaml up -d
docker compose -p ai-rc-synthetic -f integration/containers/compose.smoke.yaml --profile check run --rm smoke-check
docker compose -p ai-rc-synthetic -f integration/containers/compose.smoke.yaml down
```

합성 DB/토큰은 전용 프로젝트가 생성합니다. 실제 키를 넣지 않습니다. `down`은 볼륨을 보존합니다. 결과 확인 뒤 담당자가 이 프로젝트의 합성 볼륨만 선택해 정리합니다. 전역 prune은 사용하지 않습니다. Console은 loopback 18811이며 실제 BE가 아닙니다.

## 준비된 서비스 환경

실제 서비스 후보는 `compose.guarded.yaml`입니다. 기존 외부 secret **파일 경로**를 설정하며 값은 명령행이나 Git에 넣지 않습니다.

| 설정 | 준비할 내용 |
|---|---|
| AI_DATABASE_NAME, EMMET_POSTGRES_VOLUME | 새 emmet4930_... DB와 사전 준비한 PG18 external volume |
| CHAT_DATABASE_URL_FILE, PROFILER_DATABASE_URL_FILE, SEARCH_DATABASE_URL_FILE | 같은 postgres:5432 DB, 서비스별 제한 역할 DSN |
| MIGRATION_DATABASE_URL_FILE, CATALOG_SYNC_DATABASE_URL_FILE | 기존 ai_migrator, ai_catalog_sync DSN |
| AI_SERVICE_TOKEN_FILE, SESSION_SECRET_FILE, QA_ADMIN_PASSWORD_FILE, DEMO_ACCESS_TOKEN_FILE | 준비된 인증 파일 |
| STORE_TOKEN_FILE, OPENROUTER_KEY_FILE | 기존 누적 비용 원장과 허가된 모델 키 |
| BUDGET_GUARDED_ORIGIN, BUDGET_EXPECTED_MAX_ATTEMPTS, BUDGET_EXPECTED_MAX_RESERVED_USD | 같은 기존 원장의 한도·예약 fence |
| PROFILING_BACKEND_BASE_URL | 합의된 BE callback origin |
| EMMET_MODEL_DIR, EMMET_MEDIA_DIR | 검증된 읽기 전용 자산 |
| BACKEND_EXPORT_URL, BACKEND_CATEGORIES_URL, BACKEND_EXPORT_TOKEN_FILE | 완전성 계약을 구현한 BE와 기존 인증 |

실제 BE export/grant가 미완료인 현재 상태에서 값을 추측해 운영 실행하지 않습니다. [복구 절차](RECOVERY.md)의 백업·복사 DB 검증 후 [운영 가이드](SINGLE_SERVER_GUIDE.md#컨테이너-실행-순서)의 config → build → postgres/mesh → migration → sync → services 순서를 따릅니다. Search는 내부, Chat/Profiler는 loopback 포트이며 public ingress는 별도 팀 결정입니다.
