# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS node
FROM ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc AS uv
FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 \
    SEARCH_INFERENCE_THREADS=2 HF_HUB_OFFLINE=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node

FROM base AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY embedding/package.json embedding/package-lock.json ./embedding/
RUN --mount=type=cache,target=/root/.npm \
    node /usr/local/lib/node_modules/npm/bin/npm-cli.js ci --prefix embedding --omit=dev --ignore-scripts --no-audit --no-fund
COPY embedding/model.lock.json ./embedding/
COPY tools/download_model.py ./tools/
RUN python tools/download_model.py

FROM base AS runtime
LABEL org.opencontainers.image.title="ai-search" \
      org.opencontainers.image.source="https://github.com/100-hours-a-week/KTB4-12th-AI"
COPY --from=dependencies /app/.venv ./.venv
COPY --from=dependencies /app/embedding ./embedding
COPY *.py ./
COPY search ./search
COPY embedding/*.mjs ./embedding/
COPY tools ./tools
COPY static ./static
COPY examples ./examples
RUN groupadd --gid 10001 search && useradd --uid 10001 --gid 10001 --create-home search \
    && mkdir -p data runtime static/assets static/images \
    && chown -R 10001:10001 data runtime static/assets static/images
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=3)"]
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]

FROM runtime AS test
USER root
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --dev --no-install-project
COPY tests ./tests
COPY examples ./examples
USER 10001:10001
CMD ["sh", "-c", "uv lock --check && python -m pytest -q -p no:cacheprovider && node --test tests/test_ui.mjs"]

FROM runtime AS production
