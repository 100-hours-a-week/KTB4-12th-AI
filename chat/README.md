# Chat API

BE가 발신자·수신자와 질문을 보내면 저장된 SSE 이벤트와 추천 상품 ID를 반환합니다. PC `stable-v4.12` 하네스를 `IntegratedHarness`로 연결하며 추천은 최대 3개, 더 적게 요청한 수를 존중합니다.

서비스의 `show_gifts`와 `list_gifts(format=brief)`는 선택한 상품 ID마다 모델이 작성한 짧은 상품 소개를 요구합니다. `reason` 필드명은 유지하되 조회 근거에 있는 상품의 특징·용도·사용 느낌을 한 문장으로 소개하며, 상품명·가격·수신자 취향·예산 적합성을 반복하지 않습니다. 취향·비선호·예산은 검색과 선택에 계속 사용하고, 일반 대화의 가격 질문에는 답할 수 있습니다. 모델 문구를 최종 `productId`/`reason` 카드까지 보존하며, 누락된 소개를 카탈로그 설명으로 대신하지 않습니다. 내부 스키마와 과거 PC 하네스의 호환 범위는 [합성 회귀 안내](TESTING.md#상품별-추천-이유-합성-회귀)를 따릅니다.

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

### 관리자 요청 추적 · 모델 연결

`/admin/qa`는 기존 관리자 로그인을 사용합니다. 목록은 원문을 숨기고 요청 ID·버전·상태·사용량을 표시합니다. 상세는 명시적으로 열며 비밀·비공개 취향·숨은 추론·도구 인자 본문을 제외합니다. 모델 입력 미리보기는 호출당 최대 24,000자, 저장된 SSE 목록은 앞 100개와 잘림 여부를 표시합니다. 상세 접근 창은 24시간이며 **DB 삭제 보존 정책이 아닙니다**. 기존 감사/대화 기록을 자동 삭제하지 않습니다. 과거에 기록하지 않은 모델 입출력은 재구성하지 않습니다. 캐시·추론 토큰은 입력·출력 합계에 다시 더하지 않습니다.

모델 연결 설정은 Chat 전용이며 Profiler를 바꾸지 않습니다. 기본 OpenRouter 연결과 기존 역할별 키는 그대로입니다. 변경 UI를 쓰려면 배포에서 다음을 준비합니다. 키 값은 화면에 입력하거나 반환하지 않습니다.

- `CHAT_LLM_CONFIG_FILE`: 기존 비공개 폴더 안의 비밀 없는 연결 설정 JSON 경로. 없으면 읽기 전용입니다.
- `CHAT_LLM_ALLOWED_BASE_URLS`: 허용한 OpenAI 호환 `/v1` 주소 목록. 원격은 HTTPS·공인 IP만 허용합니다. 로컬 합성 시험은 `local/develop`의 명시된 loopback 주소만 허용합니다. 연결은 검증한 IP에 고정하고 원래 Host/TLS 이름을 유지하며 리다이렉트·환경 프록시를 사용하지 않습니다.
- `CHAT_LLM_SECRET_REFS`: 필요한 경우 **이미 주입된** 인증 환경변수 이름 목록. OpenRouter 키를 Modal로 자동 전달하지 않습니다.

Modal/vLLM 모델은 `/chat/completions`의 자동 도구 선택·도구 JSON 인자·SSE 완료 계약을 지원해야 합니다. 실제 주소와 모델 ID, 인증 방식/기존 참조, 제한 시간을 선택한 뒤 명시적 합성 호환 검증을 실행하고 저장합니다. 검증은 모델 요청 1회를 발생시킬 수 있으며 JSON Schema 전체 지원이나 추천 품질을 증명하지 않습니다. 이 변경의 개발 검증은 로컬 합성 서버만 사용했습니다. 설정은 revision 비교·파일 잠금·원자적 교체로 발행하고 기존 요청은 기존 연결로 끝납니다. 최근 5개 설정으로 되돌리기는 새 revision을 발행합니다. 검증 결과는 해당 시점의 관측이며 이후 제공자 가용성을 보장하지 않습니다.

Modal 준비 시 대시보드에는 `https://<배포 endpoint>/v1`, vLLM의 `--served-model-name`에 지정한 모델 ID, `none` 또는 기존 Bearer 비밀 참조를 넣습니다. 서버는 모델에 맞는 chat template, `--enable-auto-tool-choice`와 `--tool-call-parser`를 준비해야 합니다. Modal 프록시의 `Modal-Key`/`Modal-Secret` 이중 헤더 인증은 현재 클라이언트가 지원하지 않으므로 해당 인증 배포를 URL만 바꿔 연결할 수는 없습니다. 기존 OpenRouter 키를 재사용해 다른 호스트로 보내지 않습니다.

설정 조회·저장은 모델을 호출하지 않습니다. 별도 동의 후 **합성 호출로 호환 확인**을 눌렀을 때만 최대 64 출력 토큰의 도구/SSE 시험을 실행합니다. 시험의 전체 제한은 15초이므로 cold start가 긴 서버는 먼저 준비되어 있어야 합니다. 서비스 요청은 연결 1–30초·읽기 1–120초와 기존 턴 전체 제한(180초)을 따릅니다. 실패·취소는 설정을 바꾸지 않고 자동 재시도하지 않습니다. 호환 확인은 추천 품질이나 실제 Modal 운영 검증이 아닙니다.

근거: [Modal OpenAI 호환 vLLM 예제](https://modal.com/docs/examples/llm_inference), [vLLM 자동 도구 호출](https://docs.vllm.ai/en/latest/features/tool_calling/).
