#!/usr/bin/env sh
set -eu

python /app/backend/sync_db.py
python /app/backend/preflight.py

exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1
