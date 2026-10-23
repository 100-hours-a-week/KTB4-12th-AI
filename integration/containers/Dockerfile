# syntax=docker/dockerfile:1
# Root build context; Dockerfile-specific deny-by-default context excludes secrets/models/private data.
FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS encoder
WORKDIR /opt/encoder
COPY product-search/embedding/package.json product-search/embedding/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
 && node -e "require('onnxruntime-node')"
COPY product-search/embedding/*.mjs product-search/embedding/model.lock.json ./

FROM ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc AS uv
FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS base13
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
 HF_HUB_OFFLINE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /workspace
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libstdc++6 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 10001 app && useradd -u 10001 -g 10001 -m app \
 && mkdir -p /runtime /run/runtime && chown -R 10001:10001 /runtime /run/runtime \
 && install -d -m 0700 -o 10001 -g 10001 /run/provider-key \
 && install -m 0600 -o 10001 -g 10001 /dev/null /run/provider-key/.volume-layout
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=encoder /usr/local/bin/node /usr/local/bin/node
COPY --from=encoder /opt/encoder /opt/encoder
# Only runtime/migration adapters; historical launchers and fake BE stay out.
COPY integration/audit_profile_database.py integration/database_identity.py integration/migrate_single_server.py integration/profile_callback.py integration/profile_contract.py integration/profiler_boundary.py integration/profiler_generation.py integration/profiler_health.py integration/profiler_rebind.py integration/profiler_runtime.py integration/search_sync.py integration/shared_search_binding.py /workspace/integration/
COPY integration/provider_key.py integration/service_token.py integration/aws_credentials.py /workspace/integration/
COPY integration/containers/entrypoint.py integration/containers/health.py /workspace/integration/containers/
COPY integration/recipient_data.py integration/recipient_profile.sql /workspace/integration/
COPY product-search/catalog_db.py /workspace/product-search/
COPY product-search/search/encoder_contract.py /workspace/product-search/search/
ENV PYTHONPATH=/workspace:/workspace/profiling/src:/workspace/product-search \
 ENCODER_CODE_ROOT=/opt/encoder ASSET_ROOT=/assets SEARCH_RUNTIME_ROOT=/runtime

FROM base13 AS profiler
COPY profiling/pyproject.toml profiling/uv.lock profiling/README.md /workspace/profiling/
RUN uv sync --project profiling --frozen --no-dev --no-install-project
COPY profiling/src /workspace/profiling/src
COPY profiling/alembic /workspace/profiling/alembic
COPY profiling/alembic.ini /workspace/profiling/
ENV PATH=/workspace/profiling/.venv/bin:$PATH
USER 10001:10001
ENTRYPOINT ["python","-m","integration.containers.entrypoint"]
CMD ["profiler"]

FROM base13 AS search
COPY product-search/pyproject.toml product-search/uv.lock /workspace/product-search/
RUN uv sync --project product-search --frozen --no-dev --no-install-project
COPY product-search/*.py /workspace/product-search/
COPY product-search/search /workspace/product-search/search
COPY product-search/static /workspace/product-search/static
# Unmodified app.py mkdirs ROOT/runtime on import even when QA uses PostgreSQL.
# It stays empty/read-only; generation and encoder writes go to the /runtime volume.
RUN mkdir -p /workspace/product-search/runtime
# app.build_assets() writes only absent files; prepare them at build time for read-only runtime.
RUN python -c "import hashlib,shutil; from pathlib import Path; root=Path('/workspace/product-search/static'); files=['app.js','app.css','guide.js','guide.css','json.js']; version=hashlib.sha256(b''.join((root/name).read_bytes() for name in files)).hexdigest()[:16]; target=root/'assets'/version; target.mkdir(parents=True,exist_ok=True); [shutil.copy2(root/name,target/name) for name in files]"
COPY product-search/tools /workspace/product-search/tools
# Shared pure product vocabulary; Profiler still owns personal-profile policy.
COPY profiling/src/profiling/__init__.py profiling/src/profiling/vocab.py /workspace/profiling/src/profiling/
COPY profiling/src/profiling/vocab_data /workspace/profiling/src/profiling/vocab_data
ENV PATH=/workspace/product-search/.venv/bin:$PATH
USER 10001:10001
ENTRYPOINT ["python","-m","integration.containers.entrypoint"]
CMD ["search"]

FROM base13 AS console
COPY console/pyproject.toml console/uv.lock /workspace/console/
RUN uv sync --project console --frozen --no-dev --no-install-project
COPY console/server.py console/admin_auth.py console/products.py console/provider_usage.py /workspace/console/
COPY console/static /workspace/console/static
ENV PATH=/workspace/console/.venv/bin:$PATH
USER 10001:10001
ENTRYPOINT ["python","-m","integration.containers.entrypoint"]
CMD ["console"]

# Preserve Chat's locked Python3.12 environment using the already installed image digest.
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS chat
WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
 PYTHONPATH=/workspace:/workspace/chat:/workspace/profiling/src PATH=/workspace/chat/.venv/bin:$PATH
COPY --from=uv /uv /usr/local/bin/uv
COPY chat/pyproject.toml chat/uv.lock /workspace/chat/
RUN uv sync --project chat --frozen --no-dev --no-install-project \
 && groupadd -g 10001 app && useradd -u 10001 -g 10001 -m app \
 && mkdir -p /run/runtime && chown 10001:10001 /run/runtime \
 && install -d -m 0700 -o 10001 -g 10001 /run/provider-key \
 && install -m 0600 -o 10001 -g 10001 /dev/null /run/provider-key/.volume-layout
COPY chat/app /workspace/chat/app
COPY profiling/src/profiling/__init__.py profiling/src/profiling/vocab.py /workspace/profiling/src/profiling/
COPY profiling/src/profiling/vocab_data /workspace/profiling/src/profiling/vocab_data
COPY chat/*.md /workspace/chat/
COPY integration/database_identity.py /workspace/integration/
COPY integration/provider_key.py integration/service_token.py integration/aws_credentials.py /workspace/integration/
COPY integration/containers/entrypoint.py integration/containers/health.py /workspace/integration/containers/
USER 10001:10001
ENTRYPOINT ["python","-m","integration.containers.entrypoint"]
CMD ["chat"]

FROM base13 AS mesh
USER 10001:10001
ENTRYPOINT ["python","-c","import signal; signal.pause()"]

# Synthetic-only image variants; production targets never COPY fake provider code or the opt-in marker.
FROM profiler AS profiler-smoke
USER root
COPY integration/tests/emmet_fixture_profiler.py /workspace/integration/tests/
RUN touch /workspace/integration/containers/smoke_only
USER 10001:10001
CMD ["profiler-smoke"]

FROM chat AS chat-smoke
USER root
RUN touch /workspace/integration/containers/smoke_only
USER 10001:10001
CMD ["chat-smoke"]

FROM console AS console-smoke
USER root
COPY integration/live_backend.py /workspace/integration/
RUN touch /workspace/integration/containers/smoke_only
USER 10001:10001

FROM profiler AS migrate
COPY chat/sql /workspace/chat/sql
COPY product-search/sql /workspace/product-search/sql
ENTRYPOINT ["python","-m","integration.migrate_single_server"]

FROM profiler-smoke AS qa-init
COPY console/admin_auth.py /workspace/console/
COPY integration/live_backend.py /workspace/integration/
COPY integration/containers/smoke_init.py /workspace/integration/containers/
USER root
COPY chat/sql /workspace/chat/sql
COPY product-search/sql /workspace/product-search/sql
COPY --from=search /workspace/product-search /workspace/product-search
ENV PYTHONPATH=/workspace:/workspace/profiling/src:/workspace/product-search:/workspace/product-search/.venv/lib/python3.13/site-packages
COPY integration/tests/fixture.json /workspace/integration/tests/
COPY integration/tests/media /workspace/integration/tests/media
RUN mkdir -p /shared && chown 10001:10001 /shared
USER 10001:10001
ENTRYPOINT ["python","-m","integration.containers.smoke_init"]

FROM profiler-smoke AS smoke-check
COPY integration/containers/smoke_check.py /workspace/integration/containers/
ENTRYPOINT ["python","-m","integration.containers.smoke_check"]
