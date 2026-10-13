# 작은 FE–BE 채팅 예제

이 패키지는 `chat/` 앱에 포함된 체험 연동 화면입니다. 화면은 체험 코드를 받고, 예제 BE는 서버의 서비스 토큰으로 대화 생성·질문·상태·SSE·취소 HTTP API를 호출합니다. 서비스 토큰은 브라우저에 전달하지 않습니다.

[채팅 앱 실행 안내](../../README.md)에 따라 Python 3.12 환경에서 주앱을 실행한 뒤, 같은 서버의 `/minimal-chat/`에서 화면을 엽니다. `/minimal-chat/test`는 모델 호출 없는 고정 응답 경로를 사용합니다. API 규칙은 [연동 명세](../../INTEGRATION.md), 무료 시나리오는 [시험 안내](../../TESTING.md)를 따릅니다.

## 독립 예제 실행

작업 디렉터리는 저장소의 `chat/`입니다. `AI_SERVICE_TOKEN`, `DEMO_ACCESS_TOKEN`, `SESSION_SECRET`을 실행 환경에 주입하고, `AI_CHAT_URL`에는 확인할 팀 채팅 서버의 HTTPS origin을 지정합니다. API 경로·query·자격증명은 URL에 넣지 않습니다. 앱은 `.env`를 자동으로 읽지 않습니다.

```sh
cd chat
uv sync --frozen
uv run --frozen python -m app.minimal_chat
```

독립 화면은 loopback `127.0.0.1:8873`에서 실행되며 일반 화면은 `/`, 무료 화면은 `/test`입니다. loopback HTTP 화면 확인에만 `COOKIE_SECURE=false`를 명시합니다. 외부 서버의 인증 통신은 HTTPS를 유지합니다. `AI_CHAT_URL`을 생략하면 기존 HF 데모가 선택되므로 팀 서버 검증에는 배포 origin을 지정해야 합니다.

예제 세션의 합성 발신자 ID와 수신자 ID 1은 제품 회원 연결이 아닙니다. 실제 BE는 인증된 회원·수신자 ID와 대화 접근 권한을 연결해야 합니다. 예제 권한 매핑은 프로세스 메모리에 있으므로 재시작 후 새 예제 세션으로 시작합니다. 상품 카드는 `productId`와 `reason`을 표시하며 실제 상품 상세·구매 연결은 담당자가 ID 계약을 확인한 뒤 추가합니다.

## 회귀 확인

같은 `chat/` 디렉터리에서 checkout에 포함된 합성 API 회귀를 실행합니다. 실제 HTTP·LLM 전송은 차단됩니다. 예제 화면의 브라우저 동작 확인은 별도입니다.

```sh
uv sync --frozen --group test
uv run --frozen pytest
```
