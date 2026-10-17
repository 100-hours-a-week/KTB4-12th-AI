# 오프라인 자산과 검증 환경 준비

아래 준비 명령은 사용자가 필요한 환경에서 실행하는 명령입니다. 이번 작업에서는 다운로드·컴파일·전역 설치를 실행하지 않았습니다. 네트워크 없이 시작하려면 console의 내장 DEMO부터 사용하세요. 실제3서비스는 Node encoder/벡터 계약을 그대로 검사합니다.

## 모델과 Node

저장소 루트에서 console Python 환경을 만든 뒤 실행합니다. 새 디렉터리에 포함된 스크립트와 lock을 사용하며 개인 소유자의 절대 경로가 필요하지 않습니다.

```sh
console/.venv/bin/python -B product-search/tools/download_model.py
npm ci --prefix product-search/embedding
export EMBEDDING_ASSETS="$(pwd)/product-search/embedding"
```

첫 명령은 아래 네 파일을 해당 공개 모델 revision에서 받아 바이트 수·SHA256이 일치할 때만 교체합니다. 기존 파일이 일치하면 재다운로드하지 않습니다. 추론 API나 자격증명을 사용하지 않습니다. 두 번째 명령은 포함된 package-lock.json으로 현재 OS/CPU용 Node 의존성을 설치하며 native runtime 준비에 네트워크가 필요할 수 있습니다. 서로 다른 CPU의 node_modules를 복사하지 않습니다. Node26.7.0 / npm11.19.0에서 기존 설치를 확인했습니다.

모델: `jinaai/jina-embeddings-v5-text-nano-retrieval`

revision: `ac5d898c8d382b17167c33e5c8af644a3519b47d`

lock의 라이선스 표기: CC-BY-NC-4.0

총 파일 크기: 140844133바이트(134.32MiB).

| models/ 파일 | 바이트 | SHA256 |
|---|---:|---|
| model_q4f16.onnx | 130307 | `2f139a9c44942032ef6fff72e792e67ed06dc15d9badef261cb99e0ec25054ca` |
| model_q4f16.onnx_data | 123503104 | `6857e07e7e93f4de8855904f86090c51a6117e855272530aaf559a284114229c` |
| tokenizer.json | 17210235 | `98d4a1d32152d6cedf85b5e88f3b205106dca1fe72aaab34e0ac13c238421069` |
| tokenizer_config.json | 487 | `6c4640d432db970b2436a4386d3ee992b99e756b62c37446c3f581c8d09cbb05` |

필요한 폴더 구조는 다음과 같습니다. .mjs와 lock은 ZIP에 포함돼 있습니다.

```text
product-search/embedding/
  model.lock.json, package.json, package-lock.json
  runtime.mjs, documents.mjs, service.mjs
  models/model_q4f16.onnx
  models/model_q4f16.onnx_data
  models/tokenizer.json
  models/tokenizer_config.json
  node_modules/onnxruntime-node/             # version1.29.0
  node_modules/@huggingface/transformers/    # version3.8.1
```

기존 검증된 embedding/ 폴더가 있으면 위 다운로드/설치 대신 EMBEDDING_ASSETS를 그 경로로 지정할 수 있습니다. 아래 check_assets는 쓰기·다운로드·모델 실행 없이 검사합니다.

## PostgreSQL과 pgvector

실측 환경은 macOS arm64, PostgreSQL18.6, pgvector0.8.6입니다. 이미 설치된 PostgreSQL 도구 폴더를 지정하고 해당 설치에 vector.control과 라이브러리가 있는지 확인합니다.

```sh
export PG_BIN="$(brew --prefix postgresql@18)/bin"
"$PG_BIN/pg_config" --version
"$PG_BIN/pg_config" --sharedir
"$PG_BIN/pg_config" --pkglibdir
console/.venv/bin/python -B console/check_assets.py \
  --pg-bin "$PG_BIN" --embedding-assets "$EMBEDDING_ASSETS"
```

pgvector는 `pg_config --sharedir`의 extension/vector.control과 `pg_config --pkglibdir`의 vector.dylib(macOS) 또는 vector.so(Linux)에 있어야 합니다. 서로 다른 PostgreSQL major의 확장을 연결하면 안 됩니다. launcher가 자체 합성 DB를 만드므로 기존 서버를 재설정할 필요는 없습니다.

PostgreSQL18이 없는 macOS에서는 Homebrew의 `brew install postgresql@18`을 준비 명령으로 사용할 수 있습니다. pgvector가 없으면 설치 권한이 있는 사용자가 다음처럼 정확한 PG_CONFIG 대상으로 준비할 수 있습니다. 아래 명령은 이번 작업에서 실행하지 않았습니다.

```sh
mkdir -p .integration
git clone --branch v0.8.6 --depth 1 https://github.com/pgvector/pgvector.git .integration/pgvector-src
make -C .integration/pgvector-src PG_CONFIG="$PG_BIN/pg_config"
make -C .integration/pgvector-src install PG_CONFIG="$PG_BIN/pg_config"
```

컴파일 도구와 PostgreSQL 개발 파일·설치 디렉터리 쓰기 권한이 필요합니다. 운영 설치를 변경할 수 없는 환경에서는 별도 시험용 PostgreSQL+pgvector를 먼저 준비합니다. check_assets가 ready=true인 뒤에만 launcher를 실행합니다.

## 브라우저 검증 환경

ZIP에는 integration/pyproject.toml과 uv.lock이 포함돼 있습니다. 저장소 루트에서 실행합니다.

```sh
uv sync --frozen --project integration
test -x /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome
```

브라우저 스크립트는 이미 설치된 macOS Chrome을 사용하며 Playwright 브라우저 다운로드를 요구하지 않습니다. 해당 Chrome이 없으면 설치 후 실행합니다. Windows의 네이티브 launcher는 fcntl 때문에 지원하지 않습니다. Linux/다른 CPU·Compose·원격 CI는 이 전달본에서 실행 검증하지 않았습니다. 새 컴퓨터 설치가 끝났다는 주장과 기존 캐시/환경을 활용한 검증을 구분합니다.
