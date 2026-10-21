# profiling 서비스 이미지 — FastAPI(7.6 접수 · 7.7 콜백) + alembic.
# 빌드:  docker build --build-context product-search=../product-search -t profiling-ai-app .
# 실행:  docker compose up -d        (ai-db → ai-migrate → ai-app 순서로 뜬다)
#
# 마이그레이션은 **이 이미지의 다른 명령**으로 돌린다(compose의 ai-migrate) — 앱 시작과 섞지 않는다.
# 앱이 여러 개로 늘어도 마이그레이션은 한 번만 돌아야 하기 때문이다.
# Python 3.13 — 팀 개발 환경 통일(09-23 결정, product-search 와 같은 판). 로컬 .python-version · pyproject requires-python 과 같이 바꾼다
# 베이스 · uv 를 다이제스트로 고정 — 같은 커밋이면 같은 이미지(인프라 요구 §4). 검색(product-search) Dockerfile 과 같은 python 다이제스트.
# 보안 패치를 받으려면 다이제스트를 일부러 올린다(태그만 쓰면 같은 이름에 다른 내용이 덮여 재현이 안 된다)
#
# 질의 인코더(임베딩, 10-16 #12) — 검색기(product-search)의 Node 인코더 · 모델을 그대로 넣는다(검색기 Dockerfile 과 같은 단계 · 같은 다이제스트).
# 빌드 컨텍스트는 profiling/ 그대로 두고, 검색기 폴더를 이름 붙은 컨텍스트로 더한다:
#   docker build --build-context product-search=../product-search -t profiling-ai-app .
# 모델은 model.lock.json 의 해시로 검증한다 — 컨텍스트에 이미 받아 둔 모델이 있으면 쓰고, 없으면 내려받는다(Hugging Face).
# 켜는 것은 실행 설정 PROFILING_EMBEDDING_DIR=/app/product-search(비우면 임베딩 꺼짐 — 이미지만으로는 켜지지 않는다).
FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS node

FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS encoder
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
WORKDIR /enc
COPY --from=product-search embedding/package.json embedding/package-lock.json embedding/model.lock.json embedding/*.mjs ./embedding/
COPY --from=product-search tools/download_model.py ./tools/
RUN --mount=type=cache,target=/root/.npm \
    node /usr/local/lib/node_modules/npm/bin/npm-cli.js ci --prefix embedding --omit=dev --ignore-scripts --no-audit --no-fund
RUN --mount=type=bind,from=product-search,source=embedding,target=/src-embedding \
    if [ -d /src-embedding/models ]; then mkdir -p embedding/models && cp /src-embedding/models/* embedding/models/; fi \
    && python tools/download_model.py

FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26

# 인코더(onnxruntime-node)가 쓰는 시스템 라이브러리 — 검색기 이미지와 같다
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node

# uv — 로컬 개발과 같은 도구·같은 uv.lock 을 쓴다. 태그를 고정해 빌드가 재현되게 한다(로컬 uv 와 같은 판).
# 빌드에는 레지스트리 두 곳이 필요하다: docker.io(python) · ghcr.io(uv).
COPY --from=ghcr.io/astral-sh/uv:0.11.15@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 1) 의존성만 먼저 — 코드가 바뀌어도 이 층은 캐시된다
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 2) 코드 — 앱(src) · 마이그레이션(alembic) · 운영 CLI(tools/catalog)
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY tools/ ./tools/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# 루트로 돌리지 않는다
RUN useradd --create-home --uid 10001 app && chown -R app:app /app

# 3) 질의 인코더 — /app/product-search/embedding (PROFILING_EMBEDDING_DIR=/app/product-search 로 켠다).
#    chown 뒤에 복사한다 — 앱은 읽기만 하므로 root 소유로 두고, chown 이 600MB 층을 한 번 더 만들지 않게
COPY --from=encoder /enc/embedding /app/product-search/embedding
USER app

EXPOSE 8000
# uvicorn 은 SIGTERM 에 lifespan 종료(httpx 클라이언트·DB 풀 정리)를 돌린다
CMD ["uvicorn", "profiling.main:app", "--host", "0.0.0.0", "--port", "8000"]
