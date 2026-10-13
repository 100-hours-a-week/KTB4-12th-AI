# 작은 FE–BE 채팅 예제

화면은 체험 코드를 받고, 예제 BE는 서버에 있는 서비스 토큰으로 제품 HTTP API를 호출합니다.

```text
FE → 예제 BE(체험 로그인·대화 권한 확인)
   → /v1/chat/conversations(서비스 Bearer) → AI
```

대화 생성·질문 PUT·상태 GET·SSE·취소 모두 제품 HTTP API를 거칩니다. 예제 BE는 AI 저장소를 직접 읽거나 쓰지 않습니다. HF에서는 이미 로그인한 체험 세션을 인증용으로만 재사용할 수 있으며, 채팅 데이터 처리는 위 HTTP 경로 그대로입니다.

- HF 화면: `/minimal-chat/`, 고정 응답 확인: `/minimal-chat/test`.
- 독립 실행: 소스 ZIP을 풀고 Python 3.10+ 환경에서 `python -m pip install -r requirements.txt`, `python -m app.minimal_chat` 순서로 실행합니다. loopback `127.0.0.1:8873`에만 실행합니다.
- 실행환경의 기존 `AI_SERVICE_TOKEN`, `DEMO_ACCESS_TOKEN`, `SESSION_SECRET`이 필요합니다. 값은 파일·코드·명령행에 넣지 않습니다. `AI_CHAT_URL`은 서버 origin이며 기본값은 기존 HF origin입니다. 쿠키는 기존 `COOKIE_SECURE` 설정을 사용하므로 실제 사용은 HTTPS 환경을 유지합니다. 설정이 없으면 시작하지 않습니다.

로컬 HTTP 확인에만 `COOKIE_SECURE=false python -m app.minimal_chat`를 사용합니다. 기존 인증값은 실행환경에서 주입되어 있어야 합니다. HF의 인증값이나 쿠키 설정을 바꾸는 절차가 아닙니다. 독립 실행 화면은 `http://127.0.0.1:8873/`, 무료 고정 응답 화면은 `http://127.0.0.1:8873/test`입니다. 외부 서버를 연결할 때는 `AI_CHAT_URL`에 HTTPS origin을 설정합니다. `/v1/chat/conversations` 경로는 코드가 붙이므로 URL에 넣지 않습니다.

`static/chat.js`는 메시지와 UUID를 보내고 text/cards 이벤트를 표시합니다. 서버 토큰은 FE에 없습니다. `server.py`는 체험 코드와 HttpOnly 서명 쿠키를 확인한 뒤, 세션이 생성한 대화만 서비스 API로 전달합니다. 요청 거절·접수 불명 때도 새 UUID로 자동 재생성하지 않습니다. 재연결은 GET snapshot과 afterEventId, 취소는 cancellation PUT을 사용합니다. 다음 질문은 앞선 턴이 끝난 뒤 같은 conversationId로 보냅니다.

이 예제의 사용자 ID는 합성 세션 ID, 수신자 ID는 1입니다. 실제 제품 BE에서는 인증된 회원과 선택한 수신자의 ID 및 대화 접근 권한으로 연결합니다. 예제 권한 매핑은 프로세스 메모리이며 재시작 후에는 새 접속·대화로 시작합니다. AI에 저장된 턴이 삭제되는 것은 아닙니다.

독립 서버는 하나의 worker로 실행합니다. 운영 회원 인증·권한과 영속 세션 저장은 제품 BE가 연결할 부분이며, 이 예제는 이미 로그인한 회원을 임의 ID로 대신하지 않습니다. 기존 체험 인증을 사용하는 연동 확인용입니다.

상품 카드는 제품 계약의 `productId`와 `reason`만 표시합니다. 이름·가격·이미지는 추측하거나 추가 조회하지 않습니다. 상품 상세를 연결할 위치는 `static/chat.js`의 카드 렌더링 주석에 있습니다. 실제 상품 조회 인증과 AI↔BE ID 매핑을 확인한 뒤 연결합니다. HF의 별도 체험 화면이 사용하는 부가 상품 정보·이미지 조회는 이 예제의 계약에 섞지 않았습니다.

고정 응답 화면은 기존 `/v1/chat/test/conversations`를 선택합니다. `/mock text`, `/mock cards`, `/mock reconnect`, `/mock delayed_tokens`, `/mock failed`, `/mock timeout`으로 텍스트·가상 카드·재연결·취소·실패를 확인할 수 있습니다. 가상 상품 ID는 실제 상품 조회에 사용하지 않습니다. 실제 화면은 유료 모델을 호출하므로 승인된 예산 범위에서 사용합니다.

ZIP의 독립 회귀 테스트는 `python -m pip install -r requirements-dev.txt`, `python -m pytest -q`로 실행합니다. 합성 테스트 인증과 MockTransport만 사용하며 외부 네트워크·LLM 호출은 차단합니다. Node.js 환경에서는 `node --test tests/test_restore.mjs`로 복구 중 입력 잠금도 검사합니다. 전체 제품 고정 응답 계약 테스트와 데스크톱·모바일 화면 검증도 무료 환경에서 완료했습니다.
