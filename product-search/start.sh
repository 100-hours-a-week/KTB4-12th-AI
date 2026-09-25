#!/bin/sh
set -eu
cd "$(dirname "$0")"
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1
echo "검색 QA: http://127.0.0.1:${SEARCH_PORT:-4325} · API 문서: /docs" >&2
exec uv run --frozen python -m uvicorn app:app --host 127.0.0.1 --port "${SEARCH_PORT:-4325}" --workers 1 --no-access-log
