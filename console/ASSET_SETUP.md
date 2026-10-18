# 자산과 환경 준비

[Console](README.md)의 세 서비스 시험용입니다. 저장소 루트에서 실행하며, 다운로드·설치는 처음 환경을 준비할 때만 필요합니다. 화면만 확인할 때는 Console의 내장 데모를 사용하세요.

## Python·모델·Node

Python은 Chat 3.12, 나머지 서비스 3.13을 사용합니다. Node 24 이상도 준비합니다.

```sh
uv sync --frozen --project chat --group test --python 3.12
uv sync --frozen --project product-search --python 3.13
uv sync --frozen --project profiling --python 3.13
uv sync --frozen --project console --group test --python 3.13
console/.venv/bin/python -B product-search/tools/download_model.py
npm ci --prefix product-search/embedding
export EMBEDDING_ASSETS="$(pwd)/product-search/embedding"
```

모델 파일·revision·크기·SHA256·라이선스는 [model.lock.json](../product-search/embedding/model.lock.json), Node 의존성은 [package-lock.json](../product-search/embedding/package-lock.json)이 기준입니다. 다운로드 도구는 파일 크기와 SHA가 맞을 때만 적용하고, 이미 일치하는 파일은 다시 받지 않습니다.

`EMBEDDING_ASSETS`에는 저장소의 encoder 코드·lock과 `models/`, 해당 OS·CPU에서 설치한 `node_modules/`가 필요합니다. 기존 검증 폴더를 재사용할 수 있지만 다른 CPU의 node_modules를 복사하지 않습니다. Compose의 `EMMET_MODEL_DIR`는 이 폴더 아래 `models/`를 가리킵니다.

## PostgreSQL과 pgvector

PostgreSQL 18 도구와 같은 major용 pgvector 확장을 준비합니다. 아래는 macOS Homebrew 경로 예시이며 다른 설치라면 `PG_BIN`을 해당 `bin` 폴더로 바꿉니다.

```sh
export PG_BIN="$(brew --prefix postgresql@18)/bin"
"$PG_BIN/pg_config" --version
console/.venv/bin/python -B console/check_assets.py \
  --pg-bin "$PG_BIN" --embedding-assets "$EMBEDDING_ASSETS"
```

검사는 다운로드나 모델 실행 없이 파일·의존성·PG 확장을 확인합니다. `ready=true`일 때 launcher를 실행하세요. launcher는 자체 합성 DB를 만드므로 기존 서버를 재설정할 필요가 없습니다.

PostgreSQL이 없다면 `brew install postgresql@18`로 설치할 수 있습니다. pgvector가 없고 설치 권한이 있는 경우의 예시는 다음과 같습니다. 기존 설치를 바꿀 수 없다면 별도 시험 환경을 준비하세요.

```sh
mkdir -p .integration
git clone --branch v0.8.6 --depth 1 https://github.com/pgvector/pgvector.git .integration/pgvector-src
make -C .integration/pgvector-src PG_CONFIG="$PG_BIN/pg_config"
make -C .integration/pgvector-src install PG_CONFIG="$PG_BIN/pg_config"
```

같은 `pg_config` 기준의 extension/vector.control과 vector.dylib(macOS) 또는 vector.so(Linux)가 필요합니다. 설치 후 `check_assets.py`를 다시 확인합니다.

## 브라우저 검사

현재 브라우저 스크립트는 macOS Chrome 경로를 사용합니다. native launcher는 `fcntl`을 사용하므로 Windows 네이티브 실행을 지원하지 않습니다.

```sh
uv sync --frozen --project integration
test -x /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome
```

이미 설치한 Chrome을 사용하며 별도 Playwright 브라우저 다운로드는 필요하지 않습니다.
