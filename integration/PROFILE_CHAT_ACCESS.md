# 저장 프로필과 실제 BE 채팅

BE의 `ChatService`는 로그인 사용자와 대화 토큰을 확인한 뒤 현재 친구 관계(또는 본인), 활성 계정, 완료된 최신 프로필 버전을 확인합니다. 분석 대기·실패·새 변경이 있으면 503으로 명시합니다. 클라이언트 요청은 기존 `message`만 허용하며 프로필·grant·사용자 ID를 추가할 수 없습니다.

BE의 `chat_profile_grants`는 대화·질문·발신자·수신자·분석 버전에 묶인 2분짜리 권한입니다. 같은 질문 재시도는 같은 grant를 사용하며, 다른 질문에서는 재사용하지 못합니다. 만료·취소·관계 삭제·계정 비활성·새 취향 변경·분석 버전 변경은 조회를 거부합니다. 이 테이블은 BE가 소유하며 AI DB 역할에 grant 생성 권한을 추가하지 않습니다.

AI는 내부 `profileAccess`를 대화 소유권과 대조하고, Profiler의 기존 내부 읽기 경로에 `conversationId`·`turnId`를 추가합니다. Profiler는 기존 BE 서비스 인증으로 `/api/internal/v1/profile-grants/verify`를 호출합니다. scoped 검증 실패 시 기존 PG grant나 수동 문맥으로 대체하지 않습니다. 모델 작업 직전에도 다시 검증합니다. 결과와 `inputSourceVersion`, `analysisSourceVersion`, 조회 출처, 분류 호환성을 질문 문맥에 보존합니다.

분석 결과에는 분석 당시 고정된 Search 세대의 `productTagsRule`·`productTagsSha256`·validator 버전을 기록합니다. 이 근거가 없는 과거 결과는 현재 분류와 같다고 추정하지 않으며 재분석이 필요합니다. 채팅 모델에는 권한 확인된 자연어 취향·비선호 카테고리·현재 태그를 구조화된 추천 데이터로 제공합니다. 서버는 그 데이터를 시스템 지시와 구분하며, 원문 전체를 답변에 불필요하게 인용하지 않도록 합니다. 모델 도구는 상품 검색·표시용이며 사람/프로필 조회 도구는 추가하지 않습니다.

## 실행 설정

- BE: 기존 서비스 토큰과 Flyway `V35` 마이그레이션. 앱 계정의 전역 권한 변경은 필요하지 않습니다. `AI_CHAT_BASE_URL`에는 `/v1/chat/conversations` 경로가 필요합니다.
- Profiler: 기존 `PROFILING_BACKEND_BASE_URL`과 `PROFILING_BE_GRANTS_ENABLED=true`.
- Chat: `CHAT_PROFILE_READER_URL`은 Profiler origin. 기본 인증은 기존 공통 BE 서비스 토큰을 요청마다 읽어 회전을 반영합니다. 환경별로 인증이 분리돼 있으면 기존 승인된 토큰을 `CHAT_PROFILE_READER_TOKEN`으로 실행 시 주입합니다. 새 키를 생성하지 않습니다.
- 네이티브 Chat: 저장 프로필 모드에서는 `profiling/src`도 `PYTHONPATH`에 포함합니다. 공식 컨테이너에는 이미 공용 태그 코드와 경로가 있습니다.
- 기본 `integration/containers/compose.yaml`은 gateway 없이 위 모드를 사용합니다. admin overlay는 선택 사항이며 합성 smoke Compose의 모의 모델 경로는 별개입니다. BE·AI 변경은 함께 배포해야 하며 SSM/Secrets Manager, S3, 외부 TLS는 별도 배포 검증 대상입니다.

## 비과금 검증

기존 환경에서 `integration/tests/run_upgrade_offline.py`와 격리 PG 실행기 `integration/tests/run_single_server_tests.py`를 사용합니다. `chat/tests/test_scoped_profile_access.py`, `test_stored_profile_search.py`, `integration/tests/test_be_grant_authority.py`는 외부 모델 호출 없이 권한·버전·분류 경계와 비공개 검색 문맥을 검증합니다. 실제 BE·FE·모델 검증의 하네스와 증거는 별도 작업 폴더에 보존합니다.
