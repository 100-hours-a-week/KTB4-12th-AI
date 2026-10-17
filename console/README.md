# AI Local Workbench · 팀 공유 안내

세 서비스의 요청·저장 상태·SSE·HTTP 오류를 확인하는 localhost 시험 도구입니다. 공유용 실행은 **무료 합성 데이터 3상품**, Chat mock, Profiler TAGGER=none입니다. 실제 검색 서버와 오프라인 encoder를 사용하지만 실제 모델 추천 품질·BE 저장·운영 배포를 검증한 도구로 설명하지 않습니다. 코드 기준은 HEAD `3c2e8f5`와 전달 패키지에 포함된 로컬 변경입니다. HEAD만 체크아웃하면 console과 최신 수정이 누락됩니다.

## 시작

가장 작은 화면 데모는 console 환경만 필요합니다.

```sh
uv sync --frozen --project console --group test
console/.venv/bin/python -B -m uvicorn console.server:app --host 127.0.0.1 --port 18790 --no-access-log
```

이 모드는 오프라인 모델·Node·PostgreSQL 없이 내장 응답을 사용합니다. console Python 환경은 준비해야 하며 DB·실제 검색·프로파일러는 실행하지 않습니다. Ctrl+C로 종료하며 메모리 기록은 초기화됩니다.

세 서비스 시험에는 chat Python3.12, search/profiler/console Python3.13, Node, PostgreSQL+동일 버전 pgvector가 필요합니다. 검증 환경은 macOS arm64 / PostgreSQL18 / Node26입니다. 다른 OS·Compose·linux/amd64는 이 안내의 검증 결과에 포함하지 않습니다.

```sh
uv sync --frozen --project chat --group test
uv sync --frozen --project product-search
uv sync --frozen --project profiling
uv sync --frozen --project console --group test
```

3서비스 모드는 합성3상품도 encoder/벡터 계약을 유지하므로 오프라인 모델134.32MiB와 현재 OS/CPU용 Node 의존성이 필요합니다. [정확한 준비 명령·버전·파일 해시·폴더 구조·PostgreSQL/pgvector 확인](ASSET_SETUP.md)을 먼저 따르세요. 포함된 download_model.py와 package-lock.json을 이용할 수 있으며, 이번 작업에서는 다운로드나 전역 설치를 하지 않았습니다. 내장 DEMO는 이 자산 없이 실행됩니다.

저장소 루트에서 환경에 맞는 기존 경로를 지정합니다.

```sh
export PG_BIN="$(brew --prefix postgresql@18)/bin"
export EMBEDDING_ASSETS="$(pwd)/product-search/embedding"
console/.venv/bin/python -B console/launch_fixture.py \
  --pg-bin "$PG_BIN" --embedding-assets "$EMBEDDING_ASSETS" \
  --console-port 18793 --state-dir .integration/share-state \
  --output .integration/share-runtime.json
```

**단일 공유 주소는 http://127.0.0.1:18793 입니다.** 다른 컴퓨터의 localhost에는 그 컴퓨터에서 위 실행기를 시작해야 합니다. 기존 MBP의18791/18792는 이전 검토 인스턴스이므로 최신 공유 주소로 안내하지 않습니다. 포트가 사용 중이면 별도 미사용 포트를 명시합니다. 실행기 출력의 callbackFixtureURL과 `.integration/share-state/fixture-state.json`의 urls.search는 해당 실행의 주소입니다. 고정 추정 포트를 쓰지 않습니다.

검색 주소는 다음처럼 확인합니다.

```sh
console/.venv/bin/python -c 'import json; print(json.load(open(".integration/share-state/fixture-state.json"))["urls"]["search"])'
```

## 5분 확인 흐름

1. **채팅 실험:** 기본 합성 요청 전송 → COMPLETED 및 turn.finished 확인. 새 시험에서 느린 시나리오 전송 → 연결 끊기 → 재연결 → ID 중복 없이 저장 상태 확인. 같은 요청 재전송은 UUID·본문을 유지합니다. 새 시험의 실패/취소는 FAILED/CANCELLED가 되고 EOF만으로 성공이 되지 않습니다.
2. **HTTP 기록:** relay200과 실제 upstream 상태를 구분하고, 시작 경과·요청 소요·SSE 읽기 기간을 확인합니다. 기본 마스킹을 유지합니다. 429/503/연결 실패/422 표시 회귀는 아래 브라우저의 합성 실패 fixture로 검증하며 실제 공급자 장애를 뜻하지 않습니다.
3. **프로파일:** 합성 취향 입력 →202 접수 → 저장 상태 DELIVERED. 이는 합성 callback 수신이며 실제 BE 저장 확인이 아닙니다. 무료 실행기 외 비용 정책이 불명인 연결은 요청을 차단합니다.
4. **검색 화면:** `.integration/share-state/fixture-state.json`의 `urls.search` 주소를 브라우저에서 열어 SYNTHETIC 검색을 실행합니다. 101/102/103의 3상품만 나옵니다. 별도의 `/v1/search` 요청에서 snapshotId와 상품 ID를 확인합니다.
5. **서버 관측:** ready/health와 읽기 전용 DB 집계를 확인합니다. 수집 실패는 unknown/stale이고 0이나 성공으로 바꾸지 않습니다.

반복 가능한 요청/실패/재시도 검사는 새 합성 UUID와 수신자 번호를 자동 생성합니다.

```sh
PYTHONPATH=. console/.venv/bin/python -B -m console.check_fixture \
  --runtime .integration/share-runtime.json --output .integration/check-before.json
```

검색 hybrid·상품 ID·snapshot409·validation422 회복, 실제 mock 채팅 SSE/실패/취소/동일 UUID 재전송, 실제 무료 프로파일러 callback400→같은 본문 재시도→DELIVERED를 검사합니다. 실제 모델 호출은 없습니다.

## 종료와 재시작

첫 실행 터미널에서 Ctrl+C: 콘솔 접수를 닫고, 채팅 PROCESSING 및 **실제 profiler running/queued**가 비기를 기다린 뒤 자기 서비스와 DB만 정상 종료합니다. 저장된 RESULT_READY는 콜백 재전송 대기라 종료를 막지 않습니다. 30초 안에 비워지지 않거나 조회가 실패하면 강제 종료 없이 UI를 복구하고 종료 거부 안내를 출력합니다. 완료 상태를 확인한 뒤 Ctrl+C를 다시 시도합니다. kill -9나 타 프로세스 종료를 사용하지 않습니다. 파일·DB·콜백 calls/modes는 삭제하지 않습니다.

같은 디렉터리를 그대로 재개합니다. 동시에 두 실행기를 띄우면 거부합니다.

```sh
console/.venv/bin/python -B console/launch_fixture.py \
  --pg-bin "$PG_BIN" --embedding-assets "$EMBEDDING_ASSETS" \
  --console-port 18793 --state-dir .integration/share-state --resume \
  --output .integration/share-runtime.json
PYTHONPATH=. console/.venv/bin/python -B -m console.check_fixture \
  --runtime .integration/share-runtime.json --previous .integration/check-before.json \
  --output .integration/check-after.json
```

서비스 DB와 콜백 상태는 보존됩니다. 브라우저/콘솔의 채팅·프로파일 요청 매핑과 HTTP 화면 이력은 메모리이며 재시작 후 초기화됩니다. 프로파일도 같은 수신자·sourceVersion·본문을 재전송하면 저장된 결과/콜백 재시도 경로로 돌아갑니다. 기존 채팅을 계속 검사하려면 보관한 동일 UUID/본문을 다시 접수해 서비스의 기존 결과를 조회합니다. 콘솔의256개 요청 상한이 찼으면 기존 상태를 지우지 말고 새 state-dir로 새 합성 시험을 시작합니다. 사용자 모델·원본 DB·기존 실행기는 수정하지 않습니다.

## 검증과 미확인 범위

```sh
PYTHONPATH=. console/.venv/bin/python -B -m pytest console/tests -q
node --test console/tests/core.test.js
integration/.venv/bin/python -B console/tests/browser_check.py \
  --url http://127.0.0.1:18793 --recipient 90101 --output .integration/browser.json
integration/.venv/bin/python -B console/tests/browser_http.py \
  --url http://127.0.0.1:18793 --live --recipient 90102 --output .integration/http.json
```

브라우저 검사는 아래 명령으로 포함된 integration 잠금 파일의 환경을 먼저 준비합니다. macOS Chrome 설치 여부는 ASSET_SETUP.md의 확인 명령을 따릅니다.

```sh
uv sync --frozen --project integration
```

브라우저 검사는 기존 Chrome과 이 Python 환경을 사용합니다. 반복할 때 새 합성 recipient 번호를 씁니다. 실제 실행 수정은 검색의 취소된 미게시 로더 회수와 프로파일러의 불확실한 advisory-lock 세션 폐기입니다. 최신 공유 인스턴스 시작 소스 해시는 share-runtime.json에 기록하며 파일/실행본 일치를 별도 확인합니다. /api/config의 build와 모든 응답의 X-Console-Build는 해당 프로세스의 서버·정적 파일 스냅샷 해시입니다. 시작 시 정적 파일을 메모리에 고정해 이후 소스 편집으로 구형 서버와 새 JS가 섞이지 않습니다. 기존 서비스 공개 API·BE 계약·원장·상한은 유지합니다.

이 3상품 시험과 별도 **4,231상품 / 보유 media16,820개** 검증을 합치지 않습니다. 일반 추가 이미지는 **7,665 URL / 7,715 관계**가 캐시 미매핑 상태이고 상세 이미지는 제외했습니다. 전수 자료와 실제 개인정보는 패키지에 없습니다. 실제 모델 추천 품질, 실제 FE/BE 종단·BE 저장, 다중 인스턴스/실제 DB 네트워크 장애, Docker Compose 전체·linux/amd64·원격 CI·운영 배포는 미확인입니다.

## 전달 파일

전달용 압축 파일은 실행 코드·잠금 파일·합성 fixture·이 안내·안전한 합성 화면만 포함합니다. `.env`, fnox, .git, .venv, 실제 모델/상품/media, 운영 설정, 상태 DB, targets.json, 개인 대화, 로그는 제외합니다. private 설정은 해당 사용자 state-dir에 mode600으로 생성되며 브라우저에 토큰을 보내지 않습니다. 전달은 사용자가 직접 수행하며 이 작업에서 push·외부 게시·배포하지 않았습니다.
