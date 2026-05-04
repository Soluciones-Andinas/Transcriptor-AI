#!/usr/bin/env bash
# Container entrypoint — Capa 1 (ALT-1: auto-migrate at boot).
#
# Runs `alembic upgrade head` against the live Postgres before exec'ing
# uvicorn. depends_on: postgres healthy in docker-compose.yml ensures
# the DB is reachable by the time we get here.
#
# `set -euo pipefail` makes any migration failure abort startup, so a
# broken migration cannot silently leave the API serving against stale
# schema.
set -euo pipefail

echo "[entrypoint] running alembic upgrade head"
alembic upgrade head

echo "[entrypoint] starting uvicorn"
exec uvicorn transcription_api.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --log-config /app/src/transcription_api/logging.json
