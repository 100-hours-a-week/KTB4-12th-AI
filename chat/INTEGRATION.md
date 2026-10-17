# 백엔드 ↔ AI 채팅 연동 명세

대화를 만들 때 senderUserId·recipientUserId를 보내고, 질문을 보낼 때 message를 보냅니다. 답변과 처리 상태는 Turn으로 반환됩니다. 상품 추천이 있으면 카드에 상품 ID와 추천 이유를 담습니다. 아래 예시는 현재 배포 API의 필수 필드로 구성한 기본 요청입니다.

## 1. 연결과 인증

- 서비스 API Base URL: `https://wjo-ai-chat-demo.hf.space/v1/chat/conversations`
- 인증: 모든 요청에 `Authorization: Bearer <AI_SERVICE_TOKEN>`
- JSON PUT: `Content-Type: application/json`; SSE GET: `Accept: text/event-stream`

BE 서버에서 호출합니다. 브라우저 교차 출처 호출을 위한 CORS 허용은 설정되어 있지 않습니다.

서비스 토큰은 호출 서비스를 인증하며 최종 사용자별 소유권을 검사하지 않습니다. 대화 조회·취소 권한은 서비스 토큰과 UUID로 확인합니다.

`senderUserId`·`recipientUserId`는 발신자·수신자를 식별하는 양의 정수입니다. API는 ID 형식과 대화에 저장된 ID의 일치를 검사하며, 실제 회원 DB의 존재 여부는 검사하지 않습니다. 대화에 연결한 두 ID는 고정되므로 수신자를 바꿀 때는 새 conversationId를 사용합니다.

## 2. 요청과 응답

JSON은 UTF-8·camelCase를 사용합니다. 대화·턴 ID는 요청 측에서 생성한 UUID v4, 사용자·상품 ID는 숫자형 정수 `1..9007199254740991`입니다. 숫자 문자열·boolean·필수 필드의 null은 받지 않습니다. 질문은 trim 후 1–200 Unicode 코드 포인트입니다. Content-Length가 20,000 bytes를 초과하는 요청은 413을 반환합니다. 정의되지 않은 필드를 보내면 400을 반환합니다.

| Method | Base URL 뒤 경로 | 응답 |
|---|---|---|
| PUT | `/{conversationId}` | 최초 201, 같은 대화 복구 200 |
| GET | `/{conversationId}` | 대화 200 |
| PUT | `/{conversationId}/turns/{turnId}` | 처리 중 202, 종료 턴 200 |
| GET | `/{conversationId}/turns/{turnId}` | 턴 200 |
| GET | `/{conversationId}/turns/{turnId}/events?afterEventId=0` | 200 text/event-stream |
| PUT | `/{conversationId}/turns/{turnId}/cancellation` | 현재 턴 200 |

대화 PUT의 필수 body:

```json
{"senderUserId":7,"recipientUserId":42}
```

턴 PUT의 필수 body:

```json
{"message":"3만원 이하 선물 추천해줘"}
```

GET과 취소 PUT에는 body를 보내지 않습니다. 이벤트 GET의 `afterEventId`는 선택이며 0 이상 정수, 기본값은 0입니다. 이벤트 GET의 다른 Query는 400을 반환합니다. 나머지 요청은 Query를 사용하지 않습니다.

턴 PUT이 202를 반환하면 질문이 저장·접수된 상태입니다. 답변은 SSE 또는 GET 턴으로 확인합니다. PROCESSING 턴은 COMPLETED·FAILED·CANCELLED 중 하나로 종료되고, 이후에는 PROCESSING으로 돌아가지 않습니다.

HTTP 성공 응답은 문자열 `message`와 대화 또는 턴을 담은 `data`로 구성됩니다. 대화에는 UUID v4 문자열 `conversationId`와 접수 순서의 `turns` 배열이 들어갑니다. turns는 빈 배열일 수 있습니다. createdAt·completedAt 같은 timestamp나 연결 사용자 ID는 응답에 포함되지 않습니다.

```json
{"message":"대화를 조회했습니다.","data":{"conversationId":"22222222-2222-4222-8222-222222222222","turns":[]}}
```

```json
{"message":"질문 상태를 조회했습니다.","data":{"turnId":"11111111-1111-4111-8111-111111111111","status":"PROCESSING","message":"3만원 이하 선물 추천해줘","blocks":[],"lastEventId":0}}
```

| Turn 필드 | 형식·포함 조건 |
|---|---|
| turnId | UUID v4 문자열, 필수 |
| status | PROCESSING, COMPLETED, FAILED, CANCELLED 중 하나, 필수 |
| message | 접수한 질문 문자열, 필수 |
| blocks | 순서가 있는 블록 배열, 필수; 빈 배열 가능 |
| lastEventId | 마지막 저장 이벤트 번호, 0 이상 정수, 필수 |
| progressMessage | 처리 중 진행 문구 문자열, 선택; 종료 시 생략 |
| error | FAILED에서만 필수인 `{message:문자열}`; 다른 상태에서 생략 |

필수 응답 필드는 항상 포함되며 null을 허용하지 않습니다. 선택 필드는 위 조건에 따라 생략됩니다. 블록 type은 text·cards 중 하나입니다. type·text, 카드의 products·productId·reason은 해당 형식에서 모두 필수입니다. 텍스트 블록은 `{"type":"text","text":"답변"}`, 카드 블록은 아래 형식을 사용합니다. 카드 블록은 턴당 최대 하나이며 상품은 중복 없이 1–3개입니다. productId는 안전한 양의 정수, reason은 trim 후 1–200자 문자열, text는 문자열입니다. 카드 없이 텍스트만으로 완료할 수도 있습니다. 답변 text·delta에는 질문의 200자 상한이 적용되지 않습니다.

```json
{"type":"cards","products":[{"productId":101,"reason":"조건에 맞는 상품입니다."}]}
```

예제의 회원·상품 ID는 형식 설명용이며 실제 DB ID와 대조한 값이 아닙니다. 카드에는 productId·reason만 포함되며 상품명·가격·이미지·재고는 포함되지 않습니다. 추천할 상품이 없으면 카드 없이 텍스트로 COMPLETED가 될 수 있습니다.

## 3. SSE·재연결·취소

SSE는 저장이 끝난 이벤트만 보냅니다. id는 턴별로 1부터 증가하고, data에는 `{message,data}` 봉투 없이 UTF-8 JSON을 담습니다. 각 이벤트는 빈 줄로 끝납니다.

| event | data | 반영 |
|---|---|---|
| progress | `{"message":"진행 문구"}` | 진행 문구 교체 |
| answer.delta | `{"delta":"답변 조각"}` | 진행 문구 제거; 마지막 블록이 text면 추가, 아니면 새 text 블록 |
| cards | `{"products":[{"productId":101,"reason":"조건에 맞는 상품입니다."}]}` | 카드 블록 추가 |
| turn.finished | 전체 최종 Turn | 상태·blocks·lastEventId를 최종 값으로 교체 |

```text
id: 1
event: answer.delta
data: {"delta":"조건에 맞는 선물을 찾아볼게요."}

id: 2
event: turn.finished
data: {"turnId":"11111111-1111-4111-8111-111111111111","status":"COMPLETED","message":"3만원 이하 선물 추천해줘","blocks":[{"type":"text","text":"조건에 맞는 선물을 찾아볼게요."}],"lastEventId":2}

```

이벤트는 id 순서대로 반영합니다. progress·delta·cards가 항상 같은 조합으로 오는 것은 아닙니다. 마지막 turn.finished에는 COMPLETED·FAILED·CANCELLED 중 하나의 status가 들어갑니다. FAILED에는 error.message가 포함되고, 실패하거나 취소한 경우에도 부분 blocks는 남습니다.

마지막으로 완전히 반영한 id를 `afterEventId`로 보내면 다음 이벤트부터 받습니다. Last-Event-ID 헤더는 사용하지 않으며, 이미 반영한 id는 건너뜁니다. 음수·정수로 해석할 수 없는 cursor는 400, 저장된 마지막 번호보다 큰 cursor는 409입니다. 이벤트가 누락됐거나 복구 상태가 불확실하면 GET 턴의 전체 응답을 반영한 뒤 lastEventId 다음부터 이어받습니다. 종료된 턴의 마지막 번호로 연결하면 이벤트 없이 끝납니다. 완료 여부는 HTTP 200이나 EOF가 아닌 턴 상태로 확인합니다.

Heartbeat는 약 10초마다 `: keep-alive` 주석과 빈 줄로 오며 id는 없습니다. BE 프록시는 버퍼링·압축을 끄고 `Content-Type: text/event-stream`, `Cache-Control: no-store, no-transform`을 유지합니다. Idle timeout은 30초보다 길게, 전체 timeout은 현재 생성 기한 180초보다 여유 있게 설정합니다. 수신 데이터는 UTF-8·줄·이벤트 경계가 완성될 때까지 누적해서 파싱합니다.

취소할 때는 body 없이 cancellation PUT을 호출합니다. 성공 응답은 `{message,data:Turn}`입니다. 완료와 취소가 겹치면 먼저 저장된 종료 상태가 유지됩니다. 따라서 취소 요청이 200이어도 응답의 status를 확인해야 합니다. 반복 취소로 종료 상태가 바뀌지는 않으며, 없는 대화·턴은 404를 반환합니다. SSE 연결을 닫거나 PUT 응답이 timeout돼도 생성은 취소되지 않습니다.

## 4. 재시도와 오류

재시도할 때는 원래 입력을 유지합니다. 대화 PUT은 같은 두 사용자 ID, 턴 PUT은 같은 conversationId·turnId와 처음 보낸 전체 body를 유지합니다. 질문은 trim, 선택 필드는 스키마의 정규화 규칙을 적용한 뒤 비교합니다. turnId는 다른 대화에도 재사용하지 않습니다. 같은 ID에 다른 입력을 보내면 409입니다. 종료된 턴에 같은 요청을 보내면 저장된 결과를 받으며 답변을 다시 생성하지 않습니다. 한 대화에는 PROCESSING 턴 하나만 허용합니다.

```json
{"message":"요청 형식이나 값이 올바르지 않습니다.","error":{"code":"INVALID_REQUEST","traceId":"33333333-3333-4333-8333-333333333333"}}
```

| HTTP | error.code | 처리 |
|---|---|---|
| 400 | INVALID_REQUEST | 필드·타입·길이 수정 |
| 401 | UNAUTHORIZED | 서비스 토큰 확인 |
| 404 | NOT_FOUND | 대화·턴 연결 확인 |
| 405 | METHOD_NOT_ALLOWED | HTTP Method 확인 |
| 409 | CONFLICT | ID 재사용·활성 턴·수신자·cursor 확인 |
| 413 | PAYLOAD_TOO_LARGE | body 크기 수정 |
| 429 | TOO_MANY_REQUESTS | 제한된 backoff 후 같은 ID/body로 복구 |
| 500 / 503 | INTERNAL_SERVER_ERROR / SERVICE_UNAVAILABLE | GET 상태 확인 후 같은 ID/body로 제한된 재시도 |

응답을 받지 못했거나 timeout이 발생하면 같은 ID로 GET 턴을 호출해 접수 여부부터 확인합니다.

| 조회 결과 | 복구 |
|---|---|
| PROCESSING | 턴 전체 응답을 반영하고 lastEventId 이후 SSE 구독 |
| COMPLETED·FAILED·CANCELLED | 저장 결과 사용; 같은 턴 재생성 없음 |
| 404 | 접수가 불확실한 경우 인증·대화 ID를 확인하고 원래 UUID·body로 PUT 재전송 |

접수 전에 한도에 걸리면 429를 받고, 접수 후 생성에 실패하면 턴이 FAILED로 종료됩니다. 429·503은 횟수를 제한해 간격을 두고 재시도하며 Retry-After가 있으면 따릅니다. 복구를 위해 새 UUID를 자동으로 만들지 않습니다. FAILED의 Turn.error에는 message만 포함됩니다. HTTP 오류의 code·traceId와는 별도입니다. 생성 기한이 지나도 FAILED로 종료됩니다. 종료된 턴에서 답변을 새로 생성하는 요청은 자동으로 보내지 않습니다. FAILED 턴의 예시는 아래와 같습니다.

```json
{"turnId":"11111111-1111-4111-8111-111111111111","status":"FAILED","message":"3만원 이하 선물 추천해줘","blocks":[],"lastEventId":1,"error":{"message":"답변 생성에 실패했습니다."}}
```
HF gateway의 502/503은 위 JSON 형식과 다를 수 있으므로 Content-Type과 HTTP 상태도 확인합니다. 오류 식별에는 error.traceId를 사용합니다.

고정 응답으로 통신을 확인하는 방법은 [연동 테스트](https://wjo-ai-chat-demo.hf.space/guide#testing)를 참고합니다.


## 5. 요청 제한과 자원 보호

생성 제한과 HTTP/SSE 자원 보호는 별도입니다. 아래는 소스 기본 설정이며 측정된 처리량이나 응답 시간 약속이 아닙니다. HF 실행 소스의 기준은 2026-10-13에 확인한 commit `1266d9693e2067a6b333eeaa0e8ec94fe266ea14`, revision `408cc2e36841671c`입니다. 이 문서의 보호 패치 적용 여부는 현재 `/healthz` revision과 해당 배포 검증 기록으로 확인합니다. 오래된 로컬 release 파일만으로 실행 버전이나 설정을 판단하지 않습니다.

| 구분 | 적용 범위·기본 설정 | 초과·복구 |
|---|---|---|
| 생성 중인 턴 | 대화당 1턴; 같은 대화 DB 전체 `CHAT_MAX_ACTIVE=4` | 같은 대화의 추가 턴은 409, DB 전체 동시 한도는 429 |
| 최근 24시간 턴 | 같은 DB 전체 `CHAT_DAILY_LIMIT=200`; 자정 초기화가 아닌 최근 24시간 | 429; 실패·취소·무료 protocolFixture도 저장한 신규 턴이면 포함 |
| 접수 전 대기 작업 | Runtime의 미접수 작업 `CHAT_MAX_PENDING_ADMISSIONS=8` | 새 작업/저장/모델 호출 전에 503과 `Retry-After`; 영속 FIFO 큐나 대기 순번은 제공하지 않음 |
| SSE 연결 | Runtime 전체 `CHAT_MAX_SSE_CONNECTIONS=32`, 같은 turnId에 `CHAT_MAX_SSE_PER_TURN=4` | 연결 시작 전 503과 `Retry-After`; 기존 커서로 다시 연결 |
| 자원 보호 재시도 간격 | `CHAT_RESOURCE_RETRY_AFTER_SECONDS=2` | 재접수 성공 시각 보장이 아닌 최소 대기 안내; 횟수 제한·간격 증가를 함께 적용 |

생성·24시간 한도는 Store를 실행하는 프로세스에서 적용합니다. 현재 HF의 bridge 구성에서는 AI 소유 NUC 저장 worker의 설정이며 HF Space 변수만 바꿔도 이 값이 바뀐다고 가정하지 않습니다. 2026-10-13 읽기 전용 확인에서 해당 저장 worker는 4/200 설정이었습니다. 접수/SSE 보호는 단일 활성 Runtime의 프로세스 설정이며 제품·무료 테스트 경로가 함께 사용합니다. 현재 실행은 단일 worker/DB lease 전제이고 worker를 늘려 용량이 비례한다고 가정하지 않습니다.

보호 기본값 8은 접수 중 저장소·모델 메타데이터를 기다리는 작업을 제한하면서 기존 생성 4건과 별도 여유를 두기 위한 값입니다. SSE의 턴당 4는 프록시·탭·재연결의 일시 중첩을 허용하고, 전체 32는 저장소 조회 연결의 상한을 두기 위한 값입니다. 설정은 양의 정수여야 하며 사용자별 업무 할당량이나 측정된 서버 용량을 뜻하지 않습니다.

이미 Runtime이 소유한 동일 UUID·동일 body의 재전송은 원래 접수 작업을 공유합니다. 저장된 종료 턴을 새로 조회하는 PUT도 접수 보호가 가득 차면 먼저 503을 받을 수 있으므로 GET 턴으로 저장 상태를 확인하고 같은 UUID/body를 유지합니다. 상한 때문에 거절된 새 요청은 턴이나 유료 호출을 만들지 않습니다. 이미 저장된 실패·취소·완료 턴은 재생성하지 않습니다.

SSE의 종료·연결 끊김·저장소 오류 때 연결 슬롯을 회수하고, 구독자와 실행 작업이 없는 턴의 알림 자원을 정리합니다. SSE를 닫아도 생성은 계속되며 취소는 기존 cancellation PUT을 사용합니다. 연결을 반복하거나 여러 탭을 열면 모델 호출 수는 늘지 않지만 저장소 조회와 연결 자원을 사용합니다. 생성 횟수 제한을 재연결 연결 수 제한으로 해석하지 않습니다.

기존 HTTP 오류의 `{message,error:{code,traceId}}`, 429 `TOO_MANY_REQUESTS`, 503 `SERVICE_UNAVAILABLE`와 최종 FAILED의 `error.message` 형식은 유지합니다. 일일 한도·비용 부족·일시 혼잡을 구분하는 새로운 오류 필드는 아직 합의하지 않았습니다. 모든 429에 Retry-After가 있다고 가정하지 않으며 202 이후 비용/공급자/저장소 오류는 HTTP 오류 대신 FAILED 또는 연결 중단으로 나타날 수 있습니다. HTTP 200/EOF로 완료를 판정하지 않고 저장된 턴 상태와 커서를 복구합니다.

사용자별 신규 턴 한도·동시 처리 한도, 공급자 계정별 요청 속도와 확장된 제한 사유 계약은 별도 합의 항목입니다. 체험 화면의 사용자별 30회/시간 PUT 제한은 BE 서비스 API의 업무 할당량이 아닙니다. 같은 ID 재시도도 체험 PUT 요청 제한에는 포함됩니다. 누적 USD 5 원장은 기존 실험 비용 계정이며 사용자 크레딧 정책이 아닙니다. 실패·취소·연결 종료만으로 미확정 모델 비용 예약을 환급하거나 초기화하지 않습니다.
