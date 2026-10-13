# 고정 응답 연동 테스트

팀 서버 검증에는 실제 배포 origin 뒤에 `/v1/chat/test/conversations`를 붙인 Base URL을 사용합니다. 모든 요청에 해당 배포의 제품 API와 같은 `Authorization: Bearer <AI_SERVICE_TOKEN>`을 사용합니다. HTTP Method, UUID, 요청·응답, SSE, 재시도와 취소 규칙은 [채팅 API 명세](INTEGRATION.md)를 따릅니다.

`https://wjo-ai-chat-demo.hf.space/v1/chat/test/conversations`는 기존 HF 데모의 보조 예시입니다. 그 주소에서의 성공은 팀 서버 배포 검증을 대신하지 않습니다.

## 호출 순서

새 conversationId로 대화 PUT → 새 turnId로 턴 PUT → SSE GET 순서로 호출합니다. 대화 body는 `{"senderUserId":7,"recipientUserId":42}`, 턴 body는 `{"message":"/mock text"}`입니다. 최종 결과는 turn.finished의 status로 확인합니다. 연결을 복구할 때는 기존 UUID·body와 마지막으로 반영한 afterEventId를 유지합니다.

고정 응답 테스트는 실제 생성·검색·취향 API를 호출하지 않습니다. 테스트 경로와 제품 경로 사이에서 같은 conversationId를 바꿔 호출하면 409입니다. 회원 ID 7·42는 합성 예시이며 실제 회원 연결을 검증한 값이 아닙니다.

## 시나리오

message로 아래 시나리오를 선택합니다. 일반 질문은 cards와 같은 결과를 받고, 없는 `/mock …` 명령은 저장하지 않고 400을 반환합니다.

| message | 결과 |
|---|---|
| `/mock cards` | COMPLETED, 텍스트와 가상 상품 카드 3개 |
| `/mock text` | COMPLETED, 한글·이모지 텍스트 |
| `/mock no_match` | COMPLETED, 결과 없음 안내 |
| `/mock long` | COMPLETED, 약 2,500자·40줄 텍스트; HTML 형태의 문자열 포함 |
| `/mock slow` | 첫 progress 후 15초 대기; 답변 전 취소 확인 |
| `/mock delayed_tokens` | 7자씩 약 0.6초 간격으로 수신; 부분 답변 취소 확인 |
| `/mock reconnect` | 첫 delta 후 4초 대기; 수신 중 재연결 확인 |
| `/mock failed` | 부분 답변을 남기고 FAILED; 오류 메시지에 [MOCK] 표시 |
| `/mock timeout` | 부분 답변 후 약 2초 대기하고 FAILED; 시간 초과 표시 |

시간은 시나리오의 대기 설정이며 응답 시간 보장은 아닙니다. timeout은 실제 생성 기한 180초를 기다리지 않고 실패 처리를 확인합니다. 상품 ID 900000001–900000003은 가상 ID이며 실제 상품 조회·구매에 사용하지 않습니다.
