# 고정 응답 연동 테스트

팀 서버 검증에는 실제 배포 origin 뒤에 `/v1/chat/test/conversations`를 붙인 Base URL을 사용합니다. 모든 요청에 해당 배포의 제품 API와 같은 `Authorization: Bearer <AI_SERVICE_TOKEN>`을 사용합니다. HTTP Method, UUID, 요청·응답, SSE, 재시도와 취소 규칙은 [채팅 API 명세](INTEGRATION.md)를 따릅니다.

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

## 상품별 추천 이유 합성 회귀

서비스용 `IntegratedHarness`의 `show_gifts`는 `product_ids`와 `reasons`를 받습니다. `reason`은 추천 논증 대신 조회 근거에 있는 상품 자체의 특징·용도·사용 느낌을 소개합니다. 예를 들어 합성 응답 `{"product_id":"101","reason":"책을 세워 두고 읽을 수 있는 받침대예요."}`를 상품 ID로 연결합니다. 상품명·가격·수신자 취향·예산 적합성을 카드 문구에 반복하지 않도록 모델에 지시하되, 사후 문장 검사는 하지 않습니다. 검색·선택의 개인화 및 일반 대화의 가격 안내는 유지합니다. 순서 차이와 중복 때문에 모델에 수정을 요청하지 않습니다. `list_gifts(format=brief)`도 같은 표시 경로를 사용하며 이름 목록은 이유를 생략합니다.

자유문 이유의 숫자·단위·예산·브랜드·적합성과 최종 선택의 후보/가격/비선호 조건을 사후 심사하지 않습니다. 검색 요청의 가격·카테고리·비선호 필터는 유지하지만 모델이 검색 밖의 유효 상품 ID를 고르면 표시 단계에서 다시 차단하지 않습니다. 상품명·가격·사진은 같은 snapshot의 실제 카탈로그 조회값을 사용합니다. 잘못되거나 없는 ID는 건너뛰고, 중복은 합치며 기존 BE 응답의 최대 3개 카드에 맞춰 직렬화합니다. 상품별 이유가 없으면 그 카드는 만들지 않습니다. 이유의 타입/1–200자, 요청 형태·snapshot 응답 ID 일치와 인증·수신자 접근·취소/SSE 경계는 유지합니다. 깨진 응답/조회 오류는 기술 오류로 끝나며 모델에게 사후 수정 요청을 보내지 않습니다. 실제 모델 요청 상한은 기존과 같은 2회입니다.

외부 AI–BE 계약은 그대로 `products: [{productId, reason}]`, 최대 3개입니다. 이미지 관리와 조회는 BE 책임입니다. 더 적은 요청 수를 따르도록 프롬프트에 지시하고 선택된 수·순서를 늘리거나 자르지 않습니다. 자연어로 요청한 수를 모델이 올바르게 해석하는지는 이 mock 검사로 증명하지 않습니다.

호환 범위: `gift_clean_io.Selection`과 과거 버전별 PC 하네스·학습 fixture는 변경하지 않습니다. 서비스 하네스만 새 내부 스키마를 사용하며, 구형 ID-only 호출에는 새 이유를 자동 생성하지 않습니다. 저장된 과거 호출 기록은 원문을 유지하고 현재 스키마를 우선하도록 안내합니다. `MockRuntime`/`ProtocolFixture`는 외부 카드 계약을 직접 사용하므로 영향이 없습니다. 실행 영수증의 어댑터 표지는 `ai-be-v1/max3/model-choice/3`입니다. 기존 `integration/tests/check_chat_tools_search.py`는 예전 전체 filters/products 프로토콜을 쓰는 별도 native 점검으로, 이 회귀에서 검증하지 않습니다.

저장소 루트에서 무료 회귀를 실행합니다. HTTP·DB 실호출 차단 플러그인을 반드시 사용하며 실제 사용자 대화·취향이나 모델 키는 사용하지 않습니다.

```sh
PYTHONPATH=chat:chat/pc_tests:profiling/src:. chat/.venv/bin/python -B -m pytest -q \
  -p no:cacheprovider -p integration.tests.offline_guard chat/tests chat/pc_tests
```

`tests/test_recommendation_reasons.py`는 수정 전 덮어쓰기 재현, 서로 다른 이유와 선택 순서, 추천·사진 목록에서 저장 상태 → GET → SSE/turn.finished 보존, 형식·길이·ID·취소·수신자 범위를 확인합니다. `tests/test_card_boundary.py`는 긴 상품 설명이 이유를 대체하지 않는지와 기존 저장/재생 경계를 확인합니다. 입력과 전달 계약을 검사하는 scripted 결정이며, 실제 모델의 자연어 품질·사실 함의·수신자 취향 추론 품질을 평가한 결과가 아닙니다. 실제 모델 검증과 배포 검증은 별도 작업입니다.

### 2026-10-26 로컬 수정 검증 기록

출발 checkout은 `work`, `bd0f057929a04c88f777061ce9d0ea52ea65a5f1`이며 작업 전 tracked/untracked 변경은 없었습니다. checkout과 상위 workspace에서 `AGENTS.md`·관련 `.agents/skills`를 찾지 못해 README, TESTING, MAINTENANCE 및 CI 검사 지침을 따랐습니다.

| 검사 | 결과 |
|---|---|
| 수정 전 덮어쓰기 합성 재현 | 예상 실패 1개: 전달한 추천 이유 대신 `상품 정보: …` 출력 |
| 신규 이유 회귀 | 81 통과 |
| 최종 `chat/tests chat/pc_tests` 전체 | 567 통과, 실패 0, skip 2 (24.29초) |
| skip 항목 | `test_error_privacy.py`의 새 disposable PG cluster, `test_recipient_context.py`의 disposable PostgreSQL 필요 항목 |
| Ruff 0.16.8, CI 규칙 `E9,F63,F7,F82` | `chat` 및 수정한 integration fixture 통과 |
| mypy 2.4.0, 변경 runtime 모듈 | `integrated_harness.py` 통과; `--follow-imports=skip --ignore-missing-imports --check-untyped-defs`, 저장소 전체 타입 검사는 아님 |
| `git diff --check` | 통과 |
| DB/native·컨테이너·운영 연동·실제 모델·배포 | 미실행 |

독립 검토자는 실제 production diff와 서비스 연결을 읽고 ID와 이유의 대응, 이유 없는 구형 호출, 사진 목록, 200자 경계, 최대 수·취소·기존 guard, 외부 계약을 점검했습니다. 별도로 offline 기존 회귀 11개 및 순수 스키마/연결 probe 9개를 통과했으며 추가 수정이 필요한 결함은 발견하지 못했습니다. 신규 테스트 전체와 문서는 독립 검토의 완료 범위에 포함하지 않았습니다. 새로운 유료 모델 호출은 0회이며 배포·main 반영 결과가 아닙니다.
