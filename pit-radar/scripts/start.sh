#!/bin/sh
# Container entry point: migrate the database, then serve (one worker: the scheduler runs in-process).
set -eu
: "${PIT_DATA_DIR:=/data}"
mkdir -p "$PIT_DATA_DIR"
python -m alembic upgrade head
exec python -m uvicorn app.main:app \
  --host 0.0.0.0 --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips "*" \
  --workers 1 --no-server-header --timeout-graceful-shutdown 20
