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

카드 입력·표시·구형 호출의 호환 범위는 [Chat README의 상품 카드 계약](README.md#상품-카드-계약)을 따릅니다.

저장소 루트에서 무료 회귀를 실행합니다. HTTP·DB 실호출 차단 플러그인을 반드시 사용하며 실제 사용자 대화·취향이나 모델 키는 사용하지 않습니다.

```sh
PYTHONPATH=chat:chat/pc_tests:profiling/src:. chat/.venv/bin/python -B -m pytest -q \
  -p no:cacheprovider -p integration.tests.offline_guard chat/tests chat/pc_tests
```

`tests/test_recommendation_reasons.py`는 수정 전 덮어쓰기 재현, 서로 다른 이유와 선택 순서, 추천·사진 목록에서 저장 상태 → GET → SSE/turn.finished 보존, 형식·길이·ID·취소·수신자 범위를 확인합니다. `tests/test_card_boundary.py`는 긴 상품 설명이 이유를 대체하지 않는지와 기존 저장/재생 경계를 확인합니다. 입력과 전달 계약을 검사하는 scripted 결정이며, 실제 모델의 자연어 품질·사실 함의·수신자 취향 추론 품질을 평가한 결과가 아닙니다. 실제 모델 검증과 배포 검증은 별도 작업입니다.

### 2026-10-26 로컬 수정 검증 기록

아래는 `bd0f057929a04c88f777061ce9d0ea52ea65a5f1`에서 시작한 당시 기록이며, 현재 버전의 새 실행 결과가 아닙니다.

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

당시 독립 검토는 ID·이유 대응, 구형 호출·사진 목록·200자 경계·최대 수·취소·외부 계약을 확인했고 기존 회귀 11개와 스키마/연결 probe 9개가 통과했습니다. 신규 테스트 전체와 문서는 독립 검토 범위 밖이었습니다. 유료 호출은 0회이며 배포·main 반영 결과가 아닙니다.
