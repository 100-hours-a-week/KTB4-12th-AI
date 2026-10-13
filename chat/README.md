# 채팅 API

BE가 인증한 발신자·수신자와 질문을 전달하면, AI가 저장한 답변과 상품 ID를 반환합니다. BE는 사용자 권한을 확인하고 `AI_SERVICE_TOKEN`을 서버 사이에서 사용합니다. 브라우저에 서비스 토큰을 배포하지 않습니다. 상세 요청·응답은 [연동 명세](INTEGRATION.md), 모델 없는 시험은 [무료 시나리오](TESTING.md)를 따릅니다.

이 소스는 HF `WJO/ai-chat-demo` commit `323d4ca02d994d7e29c748d3205c02b727f55a8f`, 코드 revision `ba5b9a4d6a68cae9`를 팀 저장소에 옮긴 것입니다. 모델·하네스·비용 예약 정책은 유지합니다. `release.json`은 가져온 코드의 식별자이며 팀 환경 배포 완료를 뜻하지 않습니다. Python 3.12가 검증 기준입니다. 팀 Python 3.13 통합과 실제 배포는 별도 검증이 필요합니다.

## 피터가 연결할 계약

- 대화·턴 ID는 UUID v4, 회원 ID는 양의 정수입니다. 새 질문에 새 turnId를 만들고, 네트워크 재시도는 같은 ID와 body를 유지합니다. `202`는 접수이며 완료가 아닙니다.
- SSE는 저장된 이벤트 ID로 `afterEventId` 이후를 재생합니다. 프록시는 buffering을 끄고 연결을 유지해야 합니다. `turn.finished`의 상태로 완료를 판단하고, EOF 때는 저장 상태와 커서를 복구합니다. 연결 종료는 생성 취소가 아니며 취소 API를 별도로 호출합니다.
- `503 SERVICE_UNAVAILABLE`에 `Retry-After`가 있으면 기다립니다. 보호 기본값은 미접수 8개·전체 SSE 32개·턴당 SSE 4개이며 사용자별 크레딧이나 처리량 약속이 아닙니다.
- 검색은 별도 서비스입니다. 반환 상품 ID·snapshot 계약을 맞춰야 합니다. 이 PR은 `product-search/`나 `profiling/`을 수정하거나 실제 팀 데이터 연결을 완료하지 않습니다. 현재 취향 입력은 턴의 선택적 `recipientContext`이며 모델이 회원 신원을 정하지 않습니다.

## 실행과 준비 확인

작업 디렉터리는 `chat/`, 엔트리포인트는 `app.main:app`, 포트는 `7860`, worker는 **1개**입니다. 다른 모듈과 같은 `app` namespace로 한 프로세스에 합치지 않습니다.

```sh
cd chat
uv sync --frozen --group test
uv run --frozen pytest
# 배포 환경이 필요한 값을 주입한 뒤 실행
uv run --frozen uvicorn app.main:app --host 127.0.0.1 --port 7860 --workers 1 --no-access-log
```

`.env.example`에는 이름만 제공합니다. 앱이 `.env`를 자동으로 읽지는 않습니다. 공통 인증에는 `AI_SERVICE_TOKEN`, `SESSION_SECRET`, `DEMO_ACCESS_TOKEN`, `QA_ADMIN_PASSWORD`가 필요합니다. HTTPS를 기본으로 하고, loopback HTTP UI 시험에서만 `COOKIE_SECURE=false`를 명시합니다.

`STORE_MODE=postgres`는 `DATABASE_URL`의 **채팅 전용 PostgreSQL 14+ DB**에 연결합니다. `Store.__init__`이 `app/store.py`의 `SCHEMA`를 트랜잭션으로 적용합니다. CREATE/ALTER 권한이 필요하며 별도 Alembic 이력은 아직 없습니다. 초기 schema 적용과 백업·운영 migration 절차는 배포 담당자가 검토해야 합니다. 기존 대화 DB나 프로파일링 DB를 시험용으로 사용하지 않습니다.

`CHAT_RUNTIME_MODE=mock`는 PostgreSQL과 인증을 그대로 사용하면서 모델·검색을 호출하지 않습니다. 운영 `live`는 추가로 `OPENROUTER_API_KEY`, `SEARCH_URL`, `SEARCH_TOKEN`과 기존 비용 계정 연결이 필요합니다. 비용 계정이 없으면 유료 요청은 실패하며 앱이 새 계정·한도·예약 환급을 만들지 않습니다. 운영 서비스 크레딧 정책은 별도 합의 사항입니다.

기존 HF는 `STORE_MODE=bridge`입니다. 같은 DB에 붙는 저장소 호스트에서 `python -m app.bridge_worker`를 실행하고 `DATABASE_URL`, `STORE_TOKEN`, `CHAT_BRIDGE_URL`을 주입해야 합니다. `http` 저장소 모드의 엔트리포인트는 `app.gateway:app`입니다. 이 선택지들의 실제 배포 연결은 이번 이관에서 실행하지 않았습니다.

`GET /healthz`는 프로세스 생존과 코드 revision만 확인합니다. `GET /readyz`는 서비스 Bearer 인증이 필요하며 저장소 health·runtime lease·검색 준비 상태가 갖춰져야 200입니다. health 200만으로 배포 성공을 판단하지 않습니다.

```sh
# 저장소 루트에서 chat/만 빌드 context로 사용
docker build -t ktb-chat ./chat
docker run --rm --env-file /path/to/chat.env -p 127.0.0.1:7860:7860 ktb-chat
```

Docker 빌드 입력은 앱·필수 패키지·공개 가이드만 허용합니다. secret 파일·테스트·QA 원문·DB·실험 자료는 이미지에 포함하지 않습니다.

## 검증 범위

테스트는 합성 데이터와 ASGI/HTTP MockTransport를 사용하고 실제 HTTP 전송을 차단합니다. 원래 HF 기준과 호환되는 13개 테스트 모듈을 가져왔습니다. 이후 로컬 버전의 동적 가격·직접 공급자 API를 대상으로 하던 3개 모듈은 이 소스의 기능이 아니므로 포함하지 않았습니다. PostgreSQL 통합 시험은 이 mock 전용 실행에서 건너뜁니다. 모델 품질·실제 DB migration·Docker 이미지 실행·BE 종단 연동은 이 무료 회귀 결과로 완료 표시하지 않습니다.
