#!/usr/bin/env bash
# Azure App Service startup command: migrate, then serve with gunicorn + uvicorn workers.
set -euo pipefail
python -m alembic upgrade head
exec gunicorn app.main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers "${WEB_CONCURRENCY:-2}" \
  --bind "0.0.0.0:${PORT:-8000}" \
  --timeout 120 --graceful-timeout 30 --keep-alive 75 \
  --access-logfile - --forwarded-allow-ips="*"
