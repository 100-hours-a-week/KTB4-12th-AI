# Chat API

BE가 인증한 발신자·수신자와 질문을 전달하면 저장형 SSE와 상품 ID를 반환합니다. PC `stable-v4.12` 하네스를 `IntegratedHarness`가 외부 AI–BE 계약에 연결합니다. 추천은 최대 3개이며 더 적게 요청한 수를 존중합니다.

통합 실행은 저장소 루트의 [quickstart](../integration/QUICKSTART.md)를 따릅니다. 이미지·DB migration·기존 누적 비용 원장은 공통 실행 가이드에서 준비합니다. Chat은 Python 3.12, worker 1개, `ai_chat` 전용 앱 역할을 사용하며 부팅 DDL을 하지 않습니다.

- [HTTP/SSE 계약](INTEGRATION.md): 같은 turnId·body 재시도, 저장 이벤트 replay, 명시적 취소, 소유권
- [합성 시나리오](TESTING.md)
- [상품별 지속 제외 출처 회귀](tests/test_exclusion_lifecycle.py)
- [PC 소스 출처](pc_tests/PROVENANCE.json)

`GET /healthz`는 생존 확인이며 인증된 `GET /readyz`는 저장소·runtime lease·검색 준비 상태를 확인합니다. ready 200은 모델 품질이나 실제 BE 연동 성공을 뜻하지 않습니다.

```sh
uv sync --project chat --frozen --group test --python 3.12
PYTHONPATH=chat:chat/pc_tests:. chat/.venv/bin/python -B -m pytest -q -p no:cacheprovider -p integration.tests.offline_guard chat/tests chat/pc_tests
```

기존 bridge/http 저장소 어댑터·QA UI·하네스 변형은 호환 코드와 회귀 증거로 보존합니다. 릴리스 후보는 postgres 저장소와 공용 Search를 사용합니다. 별도 HF 배포나 Chat 공유 브랜치는 변경하지 않습니다.
