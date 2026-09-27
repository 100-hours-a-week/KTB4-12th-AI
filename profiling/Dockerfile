# profiling 서비스 이미지 — FastAPI(7.6 접수 · 7.7 콜백) + alembic.
# 빌드:  docker build -t profiling-ai-app .
# 실행:  docker compose up -d        (ai-db → ai-migrate → ai-app 순서로 뜬다)
#
# 마이그레이션은 **이 이미지의 다른 명령**으로 돌린다(compose의 ai-migrate) — 앱 시작과 섞지 않는다.
# 앱이 여러 개로 늘어도 마이그레이션은 한 번만 돌아야 하기 때문이다.
FROM python:3.12-slim-bookworm

# uv — 로컬 개발과 같은 도구·같은 uv.lock 을 쓴다. 태그를 고정해 빌드가 재현되게 한다(로컬 uv 와 같은 판).
# 빌드에는 레지스트리 두 곳이 필요하다: docker.io(python) · ghcr.io(uv).
COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /uvx /bin/

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
USER app

EXPOSE 8000
# uvicorn 은 SIGTERM 에 lifespan 종료(httpx 클라이언트·DB 풀 정리)를 돌린다
CMD ["uvicorn", "profiling.main:app", "--host", "0.0.0.0", "--port", "8000"]
