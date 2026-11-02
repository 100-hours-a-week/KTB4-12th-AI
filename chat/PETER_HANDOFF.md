# AI Chat 대시보드 연결 요청

> develop 반영 후 전달용입니다. 실제 push·배포가 완료됐다는 기록은 아닙니다.

Peter, AI 대시보드에서 OpenRouter와 준비된 Modal 모델을 선택해서 쓸 수 있도록 연결을 부탁해요. 이번 코드는 모델 연결·전환 기능이고, Modal에 모델을 배포하는 코드는 포함하지 않았어요.

## 준비된 기능

기본은 기존 OpenRouter API예요. Modal 연결을 저장한 뒤 대시보드에서 추론용 Proxy Token(`TokenID.TokenSecret`, 예: `wk-….ws-…`)을 입력하고 별도로 적용합니다. 키 입력·저장·적용 자체는 모델을 호출하지 않아요. 키는 서버 메모리에만 두며 파일·DB·환경변수·브라우저 저장소에는 저장하지 않습니다.

Modal을 끄거나 서버를 재시작하면 키 없이 OpenRouter로 돌아갑니다. 진행 중 요청은 기존 연결로 끝나며, Modal 호출 실패 시 OpenRouter로 자동 재시도하지 않아요. OpenRouter 기본 모델·역할별 키가 준비되지 않았다면 차단 상태를 표시합니다. 이 전환은 API 라우팅이며 Modal GPU의 시작·종료나 호스팅 요금을 제어하지 않습니다.

요청 상세에서는 기록된 모델과 설정 revision, 마스킹된 입출력·검색·SSE 흐름을 볼 수 있어요. 호환 확인은 사용자가 동의해 직접 실행하며 비용이 발생할 수 있습니다. 과거 확인 결과가 현재 가용성을 보장하지는 않고, 자동 fallback이나 유료 상태 확인은 하지 않아요.

## 내가 등록할 비밀값

Secrets Manager에는 제가 아래 값을 등록할게요. Peter는 각 값을 Chat 런타임의 같은 이름 환경변수로 주입되도록 연결해 주세요. 등록만으로 앱에 자동 전달되지는 않습니다.

| 이름 | 용도 |
| --- | --- |
| `QA_ADMIN_PASSWORD` | 대시보드 로그인 비밀번호 |
| `SESSION_SECRET` | 비밀번호와 별개인 기존 세션 서명용 비밀값 |

로그인에는 관리자 비밀번호를 쓰고 IAM access key는 쓰지 않아요. 기존 OpenRouter 인증 경로도 그대로 사용합니다. Modal 키는 Secrets Manager에 추가하지 않고, 필요한 때 대시보드에서 추론 endpoint용 키만 입력해요. AWS 키나 Modal 계정 관리 키는 입력하지 않습니다.

## Peter가 연결할 최소 작업

### 대시보드 접속 경로

제안 주소는 `https://seonjalal.com/admin/qa`예요. 기존 승인된 접근 범위를 유지하면서 다음 경로만 AI Chat으로 연결해 주세요.

- `/admin/*`
- `/static/admin.css`, `/static/http.js`, `/static/admin.js`
- 신고 관리도 사용할 경우 `/static/guide.css`, `/static/reports-admin.js`

`/static/*` 전체를 AI로 보내면 FE 자산을 가로챌 수 있어요. 기존 `/`의 FE와 `/api/`의 BE 라우팅은 유지해 주세요. 이 구성에서 대시보드의 `/` 채팅 테스트 링크는 AI 채팅 화면이 아닌 FE 홈으로 이동합니다. 팀 BE 관리자 화면과도 별개예요.

staging 기본 프록시는 Cookie/Origin을 제거하므로 관리자 경로는 Host·Origin·Cookie·Set-Cookie가 올바로 전달되어야 해요. `chat_admin` 쿠키의 host-only, Path=/, Secure, HttpOnly, SameSite=Lax 설정도 유지해 주세요.

### 모델 연결과 저장 경로

- `CHAT_LLM_ALLOWED_BASE_URLS`: 승인된 Modal `https://<model-host>/v1` 주소를 등록해 주세요. 대시보드 주소와 모델 추론 주소는 서로 다릅니다.
- `CHAT_LLM_CONFIG_FILE`: private하고 쓰기 가능한 영속 마운트의 JSON 경로를 지정해 주세요. `/run/runtime` tmpfs나 읽기 전용 `/shared`·`/media`로는 설정을 보존할 수 없어요. 기존 마운트가 없다면 작은 설정 디렉터리 연결과 호스트 교체 시 보존 방법이 필요합니다.

현재 지원 범위는 **Chat 한 인스턴스·한 워커**예요. 메모리 키는 다른 프로세스와 공유하지 않습니다. 파일 공유만으로 여러 인스턴스나 워커의 설정 동기화를 보장하지 않습니다.

Modal 서버의 model ID(served-model-name), tool calling과 SSE 지원도 확인해 주세요. 현재 Modal Proxy Token의 `TokenID.TokenSecret`을 Bearer로 보내는 방식입니다. 별도 Modal Secret·IAM 권한 추가는 필요하지 않아요. 기존 비밀 참조 방식의 프로필은 메모리 키 방식으로 다시 저장해야 합니다.

### 기존 DB 준비 후 앱 배포

연결 프로필 기능 자체의 새 DB migration은 없어요. 다만 **이미지만 교체한다고 기존 DB가 갱신되지는 않으므로**, 같은 릴리스의 기존 `ai-migrate` 이미지로 먼저 migration을 실행해 주세요. 이미지 ENTRYPOINT는 `python -m integration.migrate_single_server`이며 전체 실행 형태는 아래와 같습니다.

```sh
python -m integration.migrate_single_server --database <승인된_DB명> --emmet
```

기존 migration 전용 `ai_migrator`와 `MIGRATION_DATABASE_URL` 또는 `MIGRATION_DATABASE_URL_FILE` 주입을 사용하고, 앱에는 DDL 권한을 주지 않습니다.

job이 정상 종료되면 이미지의 `integration/recipient_profile.sql`과 DB의 실제 `chat_recipient_snapshot(uuid)` 정의·실행 권한을 대조해 주세요. `changed=false`는 함수가 이미 있었다는 뜻이지 최신 정의를 확인했다는 뜻은 아니에요. 기존 함수가 있어도 migration은 SQL을 다시 실행합니다. 함수 존재나 Alembic `0011`·`single-server/1`만으로도 최신 정의를 확인할 수는 없습니다.

## 배포 후 함께 확인할 것

같은 릴리스 Chat 이미지를 배포한 뒤 image digest, `/healthz`, 인증된 `/readyz`와 응답의 release/code 식별을 확인하면 됩니다. `X-App-Revision`은 release label이라 항상 Git SHA인 것은 아니에요. 대시보드 로그인과 저장된 연결의 선택·적용·복귀도 확인해 주세요.

현재 운영 AWS·DB·배포 이미지와 실제 Modal 가용성은 확인하지 않았어요. 이 문서만으로 기존 운영 취향 문제가 해결됐다고 판단한 것은 아닙니다.
