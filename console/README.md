# 로컬 시험 Console

Chat·Profiler·Search의 요청·SSE·저장 상태를 확인하는 도구입니다. 내장 화면 데모와 합성 3상품 서비스 시험을 제공합니다. 통합 서버 실행은 [통합 안내](../integration/README.md)를 따릅니다.

## 화면만 열기

저장소 루트에서 실행합니다. Python 3.13과 uv가 필요하며 모델·Node·PostgreSQL은 사용하지 않습니다.

```sh
uv sync --frozen --project console --group test --python 3.13
export CONSOLE_ADMIN_HASH_FILE=/absolute/private/path/console-admin.hash
console/.venv/bin/python -B -m console.admin_auth --output "$CONSOLE_ADMIN_HASH_FILE"
export AI_ENVIRONMENT=local
export OPENROUTER_MANAGED_KEY_DIR=/absolute/private/path/credentials
export BE_SERVICE_TOKEN_DIR="$OPENROUTER_MANAGED_KEY_DIR"
install -d -m 0700 "$OPENROUTER_MANAGED_KEY_DIR"
console/.venv/bin/python -B -m uvicorn console.server:app --host 127.0.0.1 --port 18790 --no-access-log --no-proxy-headers
```

터미널에서 사용자 본인이 새 비밀번호를 두 번 입력하면 비공개 폴더와 `0600` 해시 파일을 만듭니다. 기존 파일은 덮어쓰지 않으므로 이미 준비했다면 생성 명령을 생략합니다. `http://127.0.0.1:18790`에서 로그인한 뒤 운영 대시보드에 키를 등록할 수 있습니다. OpenRouter 키가 없어도 이 관리 화면은 열리지만, 관리자 해시가 없으면 보호된 화면과 API는 닫힙니다. 내장 데모는 합성 응답이며 실제 AI와 연결하려면 아래 대상 설정과 같은 키 폴더를 연결합니다. Ctrl+C로 종료하며 화면의 HTTP 이력은 메모리에만 남습니다.

이 로컬 절차에는 AWS가 필요하지 않습니다. 실행 변수는 저장소 밖의 비공개 `.env`에서 셸이나 실행 도구로 주입해도 되며 Console이 `.env`를 자동으로 읽지는 않습니다. 비밀번호·해시·세션 파일은 Git과 이미지에 넣지 않습니다. AWS는 같은 설정을 공급하는 선택 경로입니다.

로그인은 최초 로그인부터 30일 동안 유지되며 재시작 후에도 이어집니다. 로그아웃·해시 교체·만료 때 무효화됩니다. 해시는 Console 실행 UID 소유여야 하며, 세션은 기본적으로 해시 파일 옆의 `.sessions.sqlite3`에 저장합니다. 해시 경로가 읽기 전용이면 `CONSOLE_ADMIN_SESSION_FILE`에 비공개 쓰기 경로를 지정하세요. 비밀번호는 배포 전에 로컬에서 준비할 수 있으며 웹 가입·초기 설정은 제공하지 않습니다.

원격 사용은 HTTPS 프록시와 정확한 `CONSOLE_ADMIN_ORIGIN`, `CONSOLE_TRUSTED_PROXY_IPS`가 필요합니다. 프록시는 Host를 보존하고 외부 전달 헤더를 지운 뒤 HTTPS와 실제 클라이언트 주소를 전달합니다. Native Uvicorn은 `--proxy-headers --forwarded-allow-ips "$CONSOLE_TRUSTED_PROXY_IPS"`로 실행하며 프록시는 개별 IP만 신뢰합니다.

Docker의 로컬 포트 전달은 `CONSOLE_ADMIN_ORIGIN=http://127.0.0.1:18811`처럼 브라우저의 정확한 loopback 주소를 명시합니다. IPv6는 `http://[::1]:포트`를 사용합니다. 포트는 호스트 loopback에만 게시하며 원격 주소의 HTTP 접속은 허용하지 않습니다.

운영 화면에는 **Chat 키·Profiler 키·공통 BE 토큰**이 있습니다. 세 프로세스의 `AI_ENVIRONMENT`를 `local`, `develop`, `production` 중 같은 값으로 지정하세요. 모델에는 서로 다른 키를 넣으며 누락·한도 오류 때 다른 서비스나 이전 공통 키를 대신 쓰지 않습니다. BE 토큰은 두 서비스의 요청 인증·Profiler 콜백에 함께 쓰므로 BE에도 같은 값을 설정합니다. Search·카탈로그 갱신에는 별도 `AI_INTERNAL_SERVICE_TOKEN` 또는 `_FILE`을 사용합니다.

기본 저장 대상은 로컬 영속 파일입니다. `OPENROUTER_MANAGED_KEY_DIR`와 `BE_SERVICE_TOKEN_DIR`에 `0700` 폴더를 지정하고 **전체 폴더**를 Console에는 쓰기 가능, Chat·Profiler에는 읽기 전용으로 연결하세요. 환경별 볼륨을 분리하고 재시작·배포 때 재사용합니다. 우선순위는 **대시보드 저장값 → 배포 `_FILE` → 배포 환경변수**입니다. 첫 저장 전에는 주입된 초기값을 쓰며, 저장 후에는 재배포한 환경변수로 덮어쓰지 않습니다. 설정한 `_FILE`이 없거나 비어 있으면 환경변수로 우회하지 않습니다. 저장값과 `.saved` 표식을 함께 보존해야 하며, 저장 파일이 손상되거나 없어지면 이전 초기값을 다시 쓰지 않습니다. 첫 저장 중 프로세스가 중단되거나 결과가 불확실하면 재입력이 필요할 수 있습니다. 관리 폴더가 없으면 외부 설정만 사용하고 화면은 읽기 전용입니다.

이전 `openrouter-key.json`·`be-service-token.json` 등은 자동 재사용하지 않습니다. 화면에서 새 값을 입력하고 전환을 확인하면 새 파일에 저장하며 이전 파일은 보존합니다. 공통 `BE_SERVICE_TOKEN`과 이전 `AI_SERVICE_TOKEN`·`PROFILING_SERVICE_TOKEN`은 같은 Chat·Profiler 프로세스에 중복 주입하지 않습니다.

기존 로컬 `/1` 공통 파일을 그대로 유지하려면 `AI_CREDENTIAL_LAYOUT=legacy-shared`를 명시하고 원래 관리 폴더를 연결합니다. 이 모드는 읽기 전용이며 키를 복사하거나 변환하지 않습니다. 분리 키 파일·변수와 함께 설정하면 거부하며, 기본 분리 모드에서 실패한 키를 공통 키로 대신 쓰지 않습니다.

저장한 값은 다시 표시하지 않습니다. **저장됨**은 저장소 쓰기 확인, **읽기 확인**은 해당 실행기가 같은 환경·revision을 읽은 상태입니다. 실행기에 닿지 못하거나 버전이 다르면 적용 미확인·대기로 표시합니다. Chat의 진행 중인 turn은 기존 키로 끝내고 다음 turn부터, Profiler는 다음 모델 요청부터 새 키를 읽습니다. 저장·상태 확인은 유료 모델 호출이나 키 한도 변경을 하지 않습니다.

형식이 잘못된 입력과 교체 전 쓰기 실패는 기존 저장값·초기 주입값을 보존합니다. 교체 후 디스크 동기화가 실패하면 저장 여부를 미확인으로 표시하므로 상태를 다시 확인합니다. 저장은 제공자 인증 성공 검사가 아니므로 **형식은 맞지만 무효인 키**까지 거르지는 않습니다. 그런 키가 저장되면 다음 요청이 실패하며 이전 키로 자동 복귀하지 않습니다.

Chat·Profiler·Console의 기본 비용 정책은 `provider-key-limits`이며 자체 비용 원장·gateway를 사용하지 않습니다. OpenRouter에서 키별 한도를 설정하세요. 기존 원장 실험만 `AI_BUDGET_MODE=guarded`를 명시하며 기존 누적액·예약은 보존합니다. Console은 서버에서 공식 [현재 키 API](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key)를 조회해 Chat·Profiler의 누적 사용액·설정 한도·남은 한도와 확인 시각을 표시합니다. 남은 한도는 계정 잔액이 아닙니다. 화면을 열거나 조회 버튼을 누를 때 키당 최대 1분에 한 번 조회하며, 5분 경과·조회 실패·무제한·미확인 값을 구분합니다. 채팅 요청은 이 조회를 기다리지 않습니다. Console 대상 파일의 `budgetMode`·`expectedChatModel`·`expectedProfilerModel`은 각각 같은 환경변수 `AI_BUDGET_MODE`·`CHAT_MODEL`·`PROFILING_TAGGER_MODEL`보다 우선합니다. 대상 파일에 명시한 값은 실제 실행기와 일치해야 하며, 모델의 기존 `expectedModel`은 서비스별 설정이 없을 때의 기본값입니다.

## Develop 설정 등록

서울 `ap-northeast-2`의 **기존 단일 Secret `/seonjalal/develop/teams/ai`**에 `OPENROUTER_API_KEY`(Chat), `PROFILING_OPENROUTER_API_KEY`(Profiler), `BE_SERVICE_TOKEN`을 필드별로 등록합니다. 앞의 두 키는 서로 다르며, 공통 토큰은 BE의 `AI_CHAT_SERVICE_TOKEN`·`PROFILING_SERVICE_TOKEN`에도 같은 값으로 연결합니다. 팀에 수정 시점을 공유하고 기존 필드는 보존하며, 등록 후에는 값 없이 이름만 전달하세요. Secret을 새로 만들거나 전체 JSON을 교체하지 않습니다.

비밀 아닌 값은 기존 배포 정의의 일반 환경변수로 넣습니다. SSM 등록은 필요하지 않습니다. Chat·Profiler·Console에 `AI_ENVIRONMENT=develop`을 같은 값으로 지정하세요. 이 값은 저장된 키·BE 토큰의 환경 일치 검사에 쓰이며 환경별 볼륨을 대신하지 않습니다.

| 변수명 | 값 |
|---|---|
| `AI_ENVIRONMENT` | `develop` |
| `AI_BUDGET_MODE` | 선택. 기본값 `provider-key-limits`; 기존 원장 실험만 `guarded` |
| `CHAT_MODEL`, `PROFILING_TAGGER_MODEL` | 선택. 통합 컨테이너 기본값은 각각 `openai/gpt-6-luna`; 변경할 때 OpenRouter 모델 ID 사용 |
| `PROFILING_BACKEND_BASE_URL` | 실제 BE 내부 origin. 로컬 기본값을 Develop 주소로 사용하지 않음 |

배포가 Secret 필드는 비밀 환경변수로, 위 값은 일반 환경변수로 주입합니다. 이미 SSM을 쓰는 배포라면 그대로 공급해도 되지만 앱이 직접 조회하지는 않습니다. `AI_CREDENTIAL_BACKEND`는 기본 `files`를 유지하고, 이전 Compose의 `_FILE`이 새 주입값을 가리지 않는지 확인합니다. 기존 DB·JWT는 변경하지 않습니다. AI 내부 토큰은 Chat·Profiler의 `AI_INTERNAL_SERVICE_TOKEN`, Search의 `AI_SERVICE_TOKEN`으로 같은 기존 값을 연결하며 BE 공통 토큰과 분리합니다. 기존 Chat의 `SESSION_SECRET`·`QA_ADMIN_PASSWORD`·`DEMO_ACCESS_TOKEN`도 필요합니다.

관리자는 위 CLI로 해시를 먼저 만듭니다. 파일 연결 대신 같은 팀 Secret의 `CONSOLE_ADMIN_HASH` 필드에 **생성한 scrypt 해시만** 직접 등록해 Console 환경변수로 주입할 수도 있습니다. 이때 `CONSOLE_ADMIN_HASH_FILE`은 해제하고 `CONSOLE_ADMIN_SESSION_FILE`에는 영속 비공개 절대 경로를 지정해야 합니다. 해시 파일을 함께 설정하면 파일이 우선하며, 파일 읽기 실패 때 환경변수로 우회하지 않습니다. 원격 화면의 `CONSOLE_ADMIN_ORIGIN`·`CONSOLE_TRUSTED_PROXY_IPS`, 서비스 주소가 담긴 `CONSOLE_TARGET_FILE`, 관리자 세션과 키 저장 폴더의 영속 마운트는 배포 담당자가 연결합니다. 대시보드에서 BE 토큰을 바꾸면 BE에도 같은 값의 적용이 필요합니다.

저장소→환경변수 주입, BE 실제 주소, 영속 볼륨·UID·프록시 연결은 별도 배포 계약이며 실제 AWS에서 검증하지 않았습니다. 대시보드 저장은 이 볼륨만 바꾸고 AWS 원본을 갱신하지 않습니다. 이전 `aws-secrets-manager` 어댑터는 전용 형식 Secret의 **읽기 전용 호환 기능**이며 위 단일 JSON Secret에 사용하지 않습니다. 이 배포에는 세 개의 ARN이나 AWS 쓰기 권한이 필요하지 않습니다.

`compose.admin.yaml`의 Console은 운영자가 준비한 대상 파일·해시 파일을 사용합니다. 두 파일의 기본 경로는 `/run/ai-console/target.json`, `/run/ai-console/admin.hash`이며 파일이나 기본 해시를 생성하지 않습니다. 없으면 Console만 시작할 수 없고, `up chat profiler`처럼 AI 서비스만 선택한 실행에는 관리자 변수가 필요하지 않습니다. 원격 Console을 시작할 때는 실제 파일 경로와 HTTPS origin·프록시 IP를 명시하세요.

## 세 서비스와 함께 실행

[자산과 환경 준비](ASSET_SETUP.md)를 마친 뒤 `PG_BIN`, `EMBEDDING_ASSETS`를 지정합니다. 새 `--state-dir`를 사용하세요. 실제 Search·CPU encoder와 Chat 고정 응답·Profiler `TAGGER=none`을 실행합니다.

런처는 새 합성 DB에 `0011`·통합 `/1` 스키마를 준비합니다. 다른 폴더에 이미 설치한 네 서비스의 `.venv`는 `--environment-root /absolute/path/to/checkout`으로 재사용할 수 있으며 실행 소스는 현재 저장소를 사용합니다.

```sh
export PG_BIN="$(brew --prefix postgresql@18)/bin"
export EMBEDDING_ASSETS="$(pwd)/product-search/embedding"
console/.venv/bin/python -B console/launch_fixture.py \
  --pg-bin "$PG_BIN" --embedding-assets "$EMBEDDING_ASSETS" \
  --console-port 18793 --state-dir .integration/share-state \
  --output .integration/share-runtime.json
```

화면은 `http://127.0.0.1:18793`입니다. 다른 컴퓨터에서는 그 컴퓨터에 실행해야 하며 포트가 사용 중이면 바꿉니다. Search 주소는 `.integration/share-state/fixture-state.json`의 `urls.search`, 콜백 주소는 `.integration/share-runtime.json`의 `callbackFixtureURL`에서 확인합니다. state-dir의 비공개 설정에는 인증 정보가 있으므로 공유하지 않습니다.

화면에서 채팅 완료·재연결·취소, 프로필 콜백, 상품 검색을 확인합니다. **답변·추천 이유 보기**를 켜면 이 화면에서 받은 결과를 읽을 수 있습니다. 완료는 EOF나 HTTP 200이 아니라 저장된 턴 상태로 판단하고 재시도에는 같은 UUID·본문을 사용합니다. 반복 가능한 합성 검사는 다음과 같습니다.

```sh
PYTHONPATH=. console/.venv/bin/python -B -m console.check_fixture \
  --runtime .integration/share-runtime.json --output .integration/check-before.json
```

## 종료와 재개

실행한 터미널에서 Ctrl+C를 누르면 신규 접수를 닫고 Chat PROCESSING과 Profiler 실행·큐가 비기를 기다립니다. 저장된 RESULT_READY는 콜백 재전송 대기이므로 종료를 막지 않습니다. 30초 내에 끝나지 않거나 상태 확인이 실패하면 종료를 거부하고 화면을 복구합니다. 작업 완료 후 다시 Ctrl+C를 누르세요. DB나 프로세스를 강제로 지워 넘기지 않습니다.

```sh
console/.venv/bin/python -B console/launch_fixture.py \
  --pg-bin "$PG_BIN" --embedding-assets "$EMBEDDING_ASSETS" \
  --console-port 18793 --state-dir .integration/share-state --resume \
  --output .integration/share-runtime.json
PYTHONPATH=. console/.venv/bin/python -B -m console.check_fixture \
  --runtime .integration/share-runtime.json --previous .integration/check-before.json \
  --output .integration/check-after.json
```

같은 state-dir의 동시 실행은 거부됩니다. DB·콜백 상태는 보존합니다. 연결 서비스에 보낸 Chat 요청의 ID·동결 본문·소유권은 기존 비공개 로그인 SQLite에 저장해 같은 로그인과 대상으로 재시작 후 조회할 수 있습니다. 로그아웃·만료·비밀번호 교체 시 지우며 다른 로그인이나 변경된 대상으로 이어 붙이지 않습니다. 로그인당 요청 256개 상한에 도달하면 진행 중인 작업이 끝난 뒤 다시 로그인하세요. HTTP 이력과 내장 화면 데모는 재시작 때 초기화됩니다.

같은 수신자에게 보내는 메시지는 한 대화로 이어집니다. `새 대화`나 수신자 변경은 대화와 입력을 초기화합니다. 같은 탭을 새로고침하면 마지막 요청의 저장 상태·이벤트를 읽어 복원하며 자동 재전송하지 않습니다. 브라우저에서는 요청 본문과 ID를 해당 로그인 세션의 탭 저장소에만 두고, 로그아웃이나 수신자 변경 때 지웁니다. 타임라인은 현재 메시지의 처리 과정을 표시합니다.

이전 버전에서 만든 대화에는 소유 기록이 없으므로 처음 전환할 때는 새 대화로 시작하세요. 첫 대화 생성 응답이 유실돼 소유권을 확인하지 못한 경우에도 새 대화가 필요합니다. 이 단계에서는 아직 모델 요청을 보내지 않습니다. 턴 접수 응답이 유실된 경우는 저장된 동일 요청으로 조회·재시도합니다.

이전 `0007/0008` 또는 준비가 누락된 state-dir은 자동으로 바꾸지 않고 재개를 거부합니다. 기존 자료를 보존하고 새 state-dir을 쓰거나 별도 복사본에서 마이그레이션을 검증하세요. 기동 도중 실패하면 이 런처가 시작한 프로세스와 DB를 종료합니다.

상품 사진은 `CONSOLE_TARGET_FILE`의 `productDisplay.backendUrl`과 `imageOrigins` 목록을 설정하면 표시됩니다. 공개 상품 목록에서 추천 ID를 확인하고 허용한 사진 서버의 이미지만 전달합니다. 로그인한 BE 상세 API는 호출하지 않으며 사진 조회 실패가 답변 결과를 지우지 않습니다.

## 회귀 검사

```sh
PYTHONPATH=. console/.venv/bin/python -B -m pytest console/tests -q
node --test console/tests/*.test.js
```

브라우저 검사 환경은 [자산 안내](ASSET_SETUP.md#브라우저-검사)에 있습니다. `console/tests/browser_check.py`와 `browser_http.py`를 실행할 때는 현재 URL과 새 합성 recipient 번호, 별도 결과 경로를 지정합니다. CLI·브라우저 검사는 비밀번호를 터미널에서 묻습니다.
