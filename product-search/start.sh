#!/bin/sh
set -eu
cd "$(dirname "$0")"
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1
exec .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port "${SEARCH_PORT:-4325}" --workers 1 --no-access-log
