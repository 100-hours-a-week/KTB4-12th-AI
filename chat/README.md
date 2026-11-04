# Chat API

BE가 발신자·수신자와 질문을 보내면 저장된 SSE 이벤트와 추천 상품 ID를 반환합니다. PC `stable-v4.12` 하네스를 `IntegratedHarness`로 연결하며 추천은 최대 3개, 더 적게 요청한 수를 존중합니다.

`show_gifts`와 `list_gifts(format=brief)`의 `reason`은 모델이 쓴 짧은 상품 소개입니다. 취향·비선호·예산은 검색과 선택에 쓰고, 카드 문구에는 상품 자체의 특징·용도를 담습니다. 표시·호환 범위는 [상품 카드 계약](#상품-카드-계약)을 따릅니다.

- [HTTP/SSE 계약](INTEGRATION.md): 요청·응답, 재시도, 취소와 소유권
- [무료 연동 테스트](TESTING.md): 고정 응답으로 FE/BE 연결 확인
- [통합 실행](../integration/README.md) · [채팅 점검](MAINTENANCE.md)
- [작은 FE–BE 예제](app/minimal_chat/README.md)

Python 3.12, worker 1개, `ai_chat` 전용 DB 역할을 사용합니다. `GET /healthz`는 생존 확인, 인증된 `GET /readyz`는 저장소·runtime lease·검색 준비 확인입니다.

이미지는 저장소 루트에서 `docker build -f integration/containers/Dockerfile --target chat .`로 빌드합니다. 예전 `chat/Dockerfile`은 더 이상 지원하지 않으며, 기동 설정은 위 통합 실행 안내를 따릅니다.

```sh
uv sync --project chat --frozen --group test --python 3.12
PYTHONPATH=chat:chat/pc_tests:profiling/src:. chat/.venv/bin/python -B -m pytest -q -p no:cacheprovider -p integration.tests.offline_guard chat/tests chat/pc_tests
```

### 상품 카드 계약

모델이 쓴 소개는 최종 `productId`/`reason` 카드까지 그대로 전달하며, 누락된 소개를 카탈로그 설명으로 대체하지 않습니다.

서비스용 `IntegratedHarness`의 `show_gifts`는 `product_ids`와 `reasons`를 받습니다. `reason`은 추천 논증 대신 조회 근거에 있는 상품 자체의 특징·용도·사용 느낌을 소개합니다. 예를 들어 합성 응답 `{"product_id":"101","reason":"책을 세워 두고 읽을 수 있는 받침대예요."}`를 상품 ID로 연결합니다. 상품명·가격·수신자 취향·예산 적합성을 카드 문구에 반복하지 않도록 모델에 지시하되, 사후 문장 검사는 하지 않습니다. 검색·선택의 개인화 및 일반 대화의 가격 안내는 유지합니다. 순서 차이와 중복 때문에 모델에 수정을 요청하지 않습니다. `list_gifts(format=brief)`도 같은 표시 경로를 사용하며 이름 목록은 이유를 생략합니다.

자유문 이유의 숫자·단위·예산·브랜드·적합성과 최종 선택의 후보/가격/비선호 조건을 사후 심사하지 않습니다. 검색 요청의 가격·카테고리·비선호 필터는 유지하지만 모델이 검색 밖의 유효 상품 ID를 고르면 표시 단계에서 다시 차단하지 않습니다. 상품명·가격·사진은 같은 snapshot의 실제 카탈로그 조회값을 사용합니다. 잘못되거나 없는 ID는 건너뛰고, 중복은 합치며 기존 BE 응답의 최대 3개 카드에 맞춰 직렬화합니다. 상품별 이유가 없으면 그 카드는 만들지 않습니다. 이유의 타입/1–200자, 요청 형태·snapshot 응답 ID 일치와 인증·수신자 접근·취소/SSE 경계는 유지합니다. 깨진 응답/조회 오류는 기술 오류로 끝나며 모델에게 사후 수정 요청을 보내지 않습니다. 실제 모델 요청 상한은 기존과 같은 2회입니다.

외부 AI–BE 계약은 그대로 `products: [{productId, reason}]`, 최대 3개입니다. 이미지 관리와 조회는 BE 책임입니다. 더 적은 요청 수를 따르도록 프롬프트에 지시하고 선택된 수·순서를 늘리거나 자르지 않습니다. 자연어로 요청한 수를 모델이 올바르게 해석하는지는 이 mock 검사로 증명하지 않습니다.

호환 범위: `gift_clean_io.Selection`과 과거 버전별 PC 하네스·학습 fixture는 변경하지 않습니다. 서비스 하네스만 새 내부 스키마를 사용하며, 구형 ID-only 호출에는 새 이유를 자동 생성하지 않습니다. 저장된 과거 호출 기록은 원문을 유지하고 현재 스키마를 우선하도록 안내합니다. `MockRuntime`/`ProtocolFixture`는 외부 카드 계약을 직접 사용하므로 영향이 없습니다. 실행 영수증의 어댑터 표지는 `ai-be-v1/max3/model-choice/3`입니다. 기존 `integration/tests/check_chat_tools_search.py`는 예전 전체 filters/products 프로토콜을 쓰는 별도 native 점검으로, 이 회귀에서 검증하지 않습니다.


### 관리자 요청 추적 · 모델 연결

`/admin/qa`는 기존 관리자 로그인을 사용합니다. 목록은 원문을 숨기고 요청 ID·버전·상태·사용량을 표시합니다. 상세는 명시적으로 열며 비밀·비공개 취향·숨은 추론·도구 인자 본문을 제외합니다. 모델 입력 미리보기는 호출당 최대 24,000자, 저장된 SSE 목록은 앞 100개와 잘림 여부를 표시합니다. 상세 접근 창은 24시간이며 **DB 삭제 보존 정책이 아닙니다**. 기존 감사/대화 기록을 자동 삭제하지 않습니다. 과거에 기록하지 않은 모델 입출력은 재구성하지 않습니다. 캐시·추론 토큰은 입력·출력 합계에 다시 더하지 않습니다.

모델 연결은 Chat 전용이며 Profiler를 바꾸지 않습니다. 기본은 기존 역할별 키를 쓰는 OpenRouter입니다. 관리자가 추론 API 주소, 모델 ID, 해당 서버의 Bearer 키를 입력하고 **입력한 연결 활성화**를 누르면 새 요청부터 적용합니다. 인증 없는 서버는 키를 비웁니다. 활성화 자체는 모델 호출이나 호환 시험을 하지 않습니다. OpenAI 호환 도구 호출과 SSE 지원은 연결할 서버에서 준비해야 합니다.

설정과 입력 키는 한 프로세스의 메모리에만 두며 파일, DB, 환경변수, 브라우저 저장소에 저장하지 않습니다. 조회 응답에는 설정만 반환하고 입력 키는 포함하지 않습니다. 입력창은 제출 즉시 비웁니다. `CHAT_LLM_CONFIG_FILE`, `CHAT_LLM_ALLOWED_BASE_URLS`, `CHAT_LLM_SECRET_REFS`는 사용하지 않으며 기존 파일도 읽거나 변경하지 않습니다. 새 URL 등록을 위한 배포나 영속 마운트는 필요 없습니다. AWS 키나 계정 관리 키는 입력하지 않습니다.

**기본 OpenRouter로 돌아가기**와 서버 재시작은 직접 연결 설정과 키를 비웁니다. 기본 OpenRouter 키가 없으면 활성화와 요청을 차단하고 준비 상태를 표시합니다. 진행 중 요청은 접수 시 연결로 끝나며 실패 시 다른 provider로 자동 재시도하지 않습니다. 전환은 외부 모델 서버나 GPU를 종료하지 않습니다. 채팅 기록은 기존 PostgreSQL에 유지됩니다.

원격 모델은 공개 HTTPS 주소를 사용합니다. URL에 인증정보를 넣지 않습니다. loopback HTTP는 `local/develop` 합성 시험에서만 허용합니다. 일반 redirect는 최대 5번 따라가며 다른 origin에는 입력 키를 전달하지 않고 사설/메타데이터 주소와 HTTPS downgrade를 차단합니다. Modal의 303 결과 조회는 GET으로 따르며 원래 POST를 다시 실행하지 않습니다. 실제 endpoint의 도구 호출/SSE 호환은 별도 검증이 필요합니다.

지원 범위는 **Chat 인스턴스 1개, worker 1개**입니다. 메모리 revision은 동시 편집과 재시작 후 오래된 화면의 변경을 거부합니다. 적용 응답 유실 시 자동 재전송하지 않고 현재 연결을 다시 조회합니다. 서비스 요청의 연결 1–30초, 읽기 1–120초와 기존 턴 전체 제한은 유지합니다.

대시보드 경로는 같은 HTTPS origin의 `/admin/qa`입니다. `/admin/*` 한 경로로 관리자 화면과 신고 관리, 정적 파일, 관리자 API를 연결합니다. FE의 `/`와 BE의 `/api/`는 유지합니다. `AI_ENVIRONMENT=production`의 관리자 Origin은 `https://www.seonjalal.com`이며 내부 Host와 분리해 검사합니다. 관리자 경로는 Origin, Cookie, Set-Cookie를 전달하고 응답 캐시를 꺼야 합니다. 기존 관리자 비밀번호와 세션 비밀 주입은 유지합니다. 모델 연결 변경 자체의 새 DB migration이나 Modal hosting 코드는 없습니다.
