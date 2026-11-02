# AI Chat 대시보드 통합 — 로컬 준비본

대상: `local/dashboard-operator-polish`의 Chat 코드. PR #41의 이전 head와 다릅니다. 아직 push·배포하지 않았습니다. Modal 모델 hosting은 포함하지 않습니다.

## AI 쪽 준비

- 저장된 연결 선택·적용, 편집 취소, 수동 OpenRouter 복귀. 다음 요청부터 적용하며 진행 중 요청은 기존 연결을 사용합니다.
- 호환 확인은 동의 후 명시적으로 실행합니다. 과거 성공은 현재 가용성 보장이 아닙니다. 자동 fallback·유료 health polling은 없습니다.
- 실패·취소 요청에서 기록된 모델/설정 revision과 마스킹된 입출력·검색/SSE 흐름을 확인합니다.
- 프로필 기능의 새 DB migration은 없습니다. 지원 범위는 **Chat 1인스턴스·worker 1개**입니다. 파일 잠금만으로 다중 호스트/worker 동기화를 보장하지 않습니다.

## Cloud에서 한 번 확인할 것

1. **관리자 접속:** 제안 주소는 `https://seonjalal.com/admin/qa`이며 실제 AWS 적용은 미확인입니다. AI `/admin/*`와 정확한 정적 파일 `/static/admin.css`, `/static/http.js`, `/static/admin.js`를 연결합니다. 신고 관리 사용 시 `/static/guide.css`, `/static/reports-admin.js`도 연결합니다. `/static/*` 전체를 AI로 보내지 말고 기존 `/api/`의 BE와 `/`의 FE 라우팅을 유지합니다. staging 기본 프록시는 Cookie/Origin을 제거하므로 관리자 경로에서 Host·Origin·Cookie·Set-Cookie 전달을 별도로 보존합니다. `chat_admin`의 host-only·Path=/·Secure·HttpOnly·SameSite=Lax 계약을 유지합니다. 팀 BE 관리자 화면과는 별개이며, 화면의 `/` 채팅 테스트 링크는 이 구성에서 FE 홈으로 갑니다.
2. **기존 인증 주입:** 사용자가 Secrets Manager에 `QA_ADMIN_PASSWORD`와 기존 `SESSION_SECRET` 값을 등록하고, Peter가 기존 Chat 런타임의 같은 이름 환경변수로 주입을 연결합니다. 로그인에는 관리자 비밀번호를 쓰며 **IAM access key를 쓰지 않습니다**. 비밀 값을 UI나 코드에 넣지 않습니다.
3. **프로필 영속 저장:** `CHAT_LLM_CONFIG_FILE`을 기존 private writable 영속 마운트의 JSON 경로로 지정합니다. `/run/runtime` tmpfs나 읽기 전용 `/shared`·`/media`로 영속성을 대신하지 않습니다. 현재 task에 쓸 수 있는 기존 마운트가 없다면 작은 설정 디렉터리 연결이 필요합니다. 호스트 교체 시 보존 방식도 확인합니다.
4. **모델 연결:** `CHAT_LLM_ALLOWED_BASE_URLS`에 승인된 `https://<model-host>/v1`을 넣습니다. 필요하면 이미 주입된 인증 변수 이름(예: `MODAL_INFERENCE_TOKEN`)을 `CHAT_LLM_SECRET_REFS`에 등록합니다. 기존 Bearer 참조만 사용하며 Modal의 `Modal-Key`/`Modal-Secret` 이중 헤더는 현재 지원하지 않습니다. 모델의 served-model-name·tool calling·SSE 지원을 확인합니다. **추론 URL과 대시보드 URL은 다릅니다.**

위 준비 뒤에는 등록 범위 내 전환마다 Cloud 수정·스크립트·재시작이 필요 없습니다. 새 secret/권한/네트워크 설정은 이 로컬 작업에서 만들지 않았습니다.

## 기존 DB migration → 앱 배포 순서

- 같은 릴리스의 기존 `ai-migrate` 이미지를 사용합니다. ENTRYPOINT는 `python -m integration.migrate_single_server`이고 인수는 `--database <승인된 DB명> --emmet`입니다. 기존 migration 전용 `ai_migrator`와 `MIGRATION_DATABASE_URL` 또는 그 `_FILE` 주입 경로를 사용합니다. 앱에 DDL 권한을 주지 않습니다.
- migration job의 정상 종료와 결과를 확인한 뒤, 배포 이미지의 `integration/recipient_profile.sql`과 **DB의 실제 `chat_recipient_snapshot(uuid)` 정의·실행 권한**을 대조합니다. 함수 존재·Alembic `0011`·`single-server/1`·`changed=false`만으로 최신 정의 적용을 증명하지 않습니다. 기존 함수가 있어도 migration은 SQL을 다시 실행하고 `changed`는 최초 존재 여부를 반영합니다.
- 성공 확인 후 같은 릴리스 Chat 이미지를 배포하고 image digest, `/healthz`, 인증된 `/readyz`, 응답의 release/code 식별을 확인합니다. `X-App-Revision`은 release label이며 항상 Git SHA인 것은 아닙니다.

운영 DB·현재 배포 이미지·실제 Modal 가용성은 이번에 검증하지 않았습니다. 기존 운영 취향 장애가 해결됐다고 판정하지 않았습니다. 실제 AWS 설정 변경과 migration 실행은 Cloud 담당 단계입니다.
