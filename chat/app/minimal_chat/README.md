# 작은 FE–BE 채팅 예제

체험 코드를 받는 화면과 Chat API를 호출하는 예제 BE입니다. 서비스 토큰은 서버에만 두며 브라우저로 보내지 않습니다. [통합 Chat](../../README.md)을 실행했다면 `/minimal-chat/`, 무료 고정 응답은 `/minimal-chat/test`에서 확인합니다.

## 독립 실행

`AI_SERVICE_TOKEN`, `DEMO_ACCESS_TOKEN`, `SESSION_SECRET`을 환경에 주입하고 `AI_CHAT_URL`을 확인할 팀 서버의 HTTPS origin으로 지정합니다. URL에 API 경로·query·자격증명을 넣지 않습니다. `.env`는 자동으로 읽지 않으며, `AI_CHAT_URL`을 생략하면 기존 HF 데모로 연결되므로 팀 서버 주소를 명시하세요.

```sh
cd chat
uv sync --frozen --python 3.12
uv run --frozen python -m app.minimal_chat
```

화면은 `http://127.0.0.1:8873/`, 무료 화면은 `/test`입니다. loopback HTTP 화면에서만 `COOKIE_SECURE=false`를 사용하고 외부 API 인증 통신은 HTTPS를 유지합니다. API 동작은 [연동 명세](../../INTEGRATION.md), 무료 시나리오는 [시험 안내](../../TESTING.md)를 따릅니다.

예제의 회원 ID는 합성 값이며 실제 회원 연결을 확인한 값이 아닙니다. 실제 BE는 회원·수신자와 대화 접근 권한을 연결해야 합니다. 예제 세션 권한은 메모리에 있으므로 재시작 후 새 세션으로 시작합니다. 카드는 `productId`와 `reason`을 표시하며 상품 상세·구매 연결은 포함하지 않습니다.

회귀는 저장소 루트에서 [Chat 검사 명령](../../README.md)을 실행합니다.
