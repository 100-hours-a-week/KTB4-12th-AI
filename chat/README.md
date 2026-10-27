# Chat API

BE가 발신자·수신자와 질문을 보내면 저장된 SSE 이벤트와 추천 상품 ID를 반환합니다. PC `stable-v4.12` 하네스를 `IntegratedHarness`로 연결하며 추천은 최대 3개, 더 적게 요청한 수를 존중합니다.

서비스의 `show_gifts`와 `list_gifts(format=brief)`는 선택한 상품 ID마다 모델이 작성한 추천 이유를 요구합니다. 사용자 조건과 조회 근거를 연결한 이유를 최종 `productId`/`reason` 카드까지 보존하며, 누락되거나 잘못된 이유를 카탈로그 설명으로 대신하지 않습니다. 내부 스키마와 과거 PC 하네스의 호환 범위는 [합성 회귀 안내](TESTING.md#상품별-추천-이유-합성-회귀)를 따릅니다.

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
