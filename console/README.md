# 로컬 시험 Console

Chat·Profiler·Search의 요청·SSE·저장 상태를 확인하는 도구입니다. 내장 화면 데모와 합성 3상품 서비스 시험을 제공합니다. 통합 서버 실행은 [통합 안내](../integration/README.md)를 따릅니다.

## 화면만 열기

저장소 루트에서 실행합니다. Python 3.13과 uv가 필요하며 모델·Node·PostgreSQL은 사용하지 않습니다.

```sh
uv sync --frozen --project console --group test --python 3.13
console/.venv/bin/python -B -m uvicorn console.server:app --host 127.0.0.1 --port 18790 --no-access-log
```

`http://127.0.0.1:18790`을 열고 Ctrl+C로 종료합니다. 기록은 메모리에만 남습니다.

## 세 서비스와 함께 실행

[자산과 환경 준비](ASSET_SETUP.md)를 마친 뒤 `PG_BIN`, `EMBEDDING_ASSETS`를 지정합니다. 새 `--state-dir`를 사용하세요. 실제 Search·CPU encoder와 Chat 고정 응답·Profiler `TAGGER=none`을 실행합니다.

```sh
export PG_BIN="$(brew --prefix postgresql@18)/bin"
export EMBEDDING_ASSETS="$(pwd)/product-search/embedding"
console/.venv/bin/python -B console/launch_fixture.py \
  --pg-bin "$PG_BIN" --embedding-assets "$EMBEDDING_ASSETS" \
  --console-port 18793 --state-dir .integration/share-state \
  --output .integration/share-runtime.json
```

화면은 `http://127.0.0.1:18793`입니다. 다른 컴퓨터에서는 그 컴퓨터에 실행해야 하며 포트가 사용 중이면 바꿉니다. Search 주소는 `.integration/share-state/fixture-state.json`의 `urls.search`, 콜백 주소는 `.integration/share-runtime.json`의 `callbackFixtureURL`에서 확인합니다. state-dir의 비공개 설정에는 인증 정보가 있으므로 공유하지 않습니다.

화면에서 채팅 완료·재연결·취소, 프로필 콜백, 상품 검색을 확인합니다. 완료는 EOF나 HTTP 200이 아니라 저장된 턴 상태로 판단하고 재시도에는 같은 UUID·본문을 사용합니다. 반복 가능한 합성 검사는 다음과 같습니다.

```sh
PYTHONPATH=. console/.venv/bin/python -B -m console.check_fixture \
  --runtime .integration/share-runtime.json --output .integration/check-before.json
```

## 종료와 재개

실행한 터미널에서 Ctrl+C를 누르면 신규 접수를 닫고 Chat PROCESSING과 Profiler 실행·큐가 비기를 기다립니다. 저장된 RESULT_READY는 콜백 재전송 대기이므로 종료를 막지 않습니다. 30초 내에 끝나지 않거나 상태 확인이 실패하면 종료를 거부하고 화면을 복구합니다. 작업 완료 후 다시 Ctrl+C를 누르세요. DB나 프로세스를 강제로 지워 넘기지 않습니다.

```sh
console/.venv/bin/python -B console/launch_fixture.py \
  --pg-bin "$PG_BIN" --embedding-assets "$EMBEDDING_ASSETS" \
  --console-port 18793 --state-dir .integration/share-state --resume \
  --output .integration/share-runtime.json
PYTHONPATH=. console/.venv/bin/python -B -m console.check_fixture \
  --runtime .integration/share-runtime.json --previous .integration/check-before.json \
  --output .integration/check-after.json
```

같은 state-dir의 동시 실행은 거부됩니다. DB·콜백 상태는 보존하지만 Console 화면의 요청 매핑과 HTTP 이력은 재시작 때 초기화됩니다. 기존 UUID·본문, 프로필은 같은 수신자·sourceVersion·본문으로 저장 결과를 조회합니다. Console의 요청 256개 상한에 도달하면 기존 상태를 지우지 말고 새 state-dir로 시험하세요.

## 회귀 검사

```sh
PYTHONPATH=. console/.venv/bin/python -B -m pytest console/tests -q
node --test console/tests/core.test.js
```

브라우저 검사 환경은 [자산 안내](ASSET_SETUP.md#브라우저-검사)에 있습니다. `console/tests/browser_check.py`와 `browser_http.py`를 실행할 때는 현재 URL과 새 합성 recipient 번호, 별도 결과 경로를 지정합니다.
