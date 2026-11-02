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

모델 연결은 Chat 전용이며 Profiler를 바꾸지 않습니다. 기본은 기존 역할별 키를 쓰는 OpenRouter입니다. 기본 모델·키 설정이 없으면 차단 상태를 표시합니다. 변경 UI에는 다음 배포 설정이 필요합니다.

- `CHAT_LLM_CONFIG_FILE`: 기존 비공개 영속 폴더의 쓰기 가능한 설정 JSON 경로. 없으면 읽기 전용입니다. 키 값은 저장하지 않습니다.
- `CHAT_LLM_ALLOWED_BASE_URLS`: 허용한 OpenAI 호환 `/v1` 주소 목록. 원격은 HTTPS·공인 IP만 허용합니다. 로컬 합성 시험은 `local/develop`의 명시된 loopback 주소만 허용합니다. 연결은 검증한 IP에 고정하며 원래 Host/TLS 이름을 유지하고 리다이렉트·환경 프록시를 사용하지 않습니다.

저장된 연결은 최대 10개입니다. URL·모델·시간 제한을 저장한 뒤 Modal 추론용 Proxy Token(`TokenID.TokenSecret`, `wk-….ws-…`)을 대시보드에 입력하고 **선택한 연결 적용**을 별도로 누릅니다. 키 입력만으로 활성화되지 않으며 입력창은 즉시 비웁니다. 키는 서버 메모리에만 두고 설정 파일·DB·환경변수·브라우저 저장소·조회 응답에는 남기지 않습니다. AWS 키나 Modal 계정 관리 키는 입력하지 않습니다.

키는 입력한 연결 설정에만 유효합니다. 설정을 바꾸면 그 설정의 키를 다시 입력해야 합니다. 기존 비밀 참조 방식의 Modal 프로필도 메모리 키 방식으로 다시 저장합니다. 선택·저장·키 입력·적용은 모델을 호출하지 않으며, 호환 확인은 활성화의 필수 조건이 아닙니다.

**Modal 끄기 · OpenRouter**는 키를 비우고 보존된 OpenRouter 설정으로 돌아갑니다. 서버 재시작도 Modal 꺼짐·키 없음으로 시작합니다. 진행 중 요청은 접수 시 연결로 끝나며, 실패 시 OpenRouter로 자동 재시도하지 않습니다. 이전 설정 복구로 Modal을 다시 켤 수는 없습니다. 이 기능은 API 라우팅이며 진행 중 호출이나 Modal GPU 호스팅 요금을 중지하지 않습니다.

최초 배포에서는 기존 관리자 인증·OpenRouter 키, 위 설정 경로와 허용 URL만 준비합니다. Modal 키를 Secrets Manager에 추가할 필요는 없습니다. 설정 폴더는 영속 쓰기가 가능해야 하며 `/run/runtime` tmpfs나 읽기 전용 `/shared`·`/media`는 대체 경로가 아닙니다. 등록된 URL 안의 전환은 UI로 처리하고, 새 URL의 허용 정책 변경은 배포 담당자가 합니다. 이 기능 자체의 새 DB migration이나 Modal hosting 코드는 없습니다.

지원 범위는 **Chat 인스턴스 1개·worker 1개**입니다. 파일 잠금·revision은 충돌하는 쓰기를 막지만 여러 프로세스의 메모리 키나 활성 상태를 공유하지 않습니다. 새 Redis·설정 서비스·자동 fallback·유료 health polling은 없습니다.

대시보드 제안 경로는 같은 HTTPS origin의 `/admin/qa`입니다. `/admin/*`와 `/static/admin.css`, `/static/http.js`, `/static/admin.js`를 연결하고 신고 관리 사용 시 `/static/guide.css`, `/static/reports-admin.js`도 연결합니다. FE의 `/`와 BE의 `/api/`는 유지하며 `/static/*` 전체를 AI로 보내지 않습니다. 관리자 경로에서 Host·Origin·Cookie·Set-Cookie를 보존해야 합니다. 일반 FE/BE 도메인에 자동으로 추가되는 경로는 아닙니다.

Modal/vLLM 서버는 모델 ID(served-model-name), 모델에 맞는 chat template, 자동 tool calling과 도구 JSON 인자·SSE 완료 계약을 지원해야 합니다. Proxy Token의 `TokenID.TokenSecret`을 단일 Bearer로 보내며 별도 이중 헤더는 사용하지 않습니다. OpenRouter 키를 다른 호스트로 전달하지 않습니다.

별도 동의 후 **명시적으로 호환 확인**을 눌렀을 때만 최대 64 출력 토큰의 합성 도구/SSE 시험을 실행합니다. 시험 제한은 15초이므로 긴 cold start는 별도로 준비해야 합니다. 서비스 요청은 연결 1–30초·읽기 1–120초와 기존 턴 전체 제한(180초)을 따릅니다. 과거 확인은 현재 가용성·추천 품질·실제 Modal 운영 성공을 보장하지 않습니다.

근거: [Modal OpenAI 호환 vLLM 예제](https://modal.com/docs/examples/llm_inference), [vLLM 자동 도구 호출](https://docs.vllm.ai/en/latest/features/tool_calling/).
