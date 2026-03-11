#!/usr/bin/env sh
set -eu

RUNTIME_DB_SYNC_MODE="${RUNTIME_DB_SYNC_MODE:-disabled}"
if [ "${RUNTIME_DB_SYNC_MODE}" != "disabled" ]; then
  python /app/backend/sync_db.py
else
  echo "Runtime DB sync disabled; using mounted /data runtime assets."
fi
python /app/backend/preflight.py

exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1
