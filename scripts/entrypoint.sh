#!/usr/bin/env bash
# Container entrypoint — Capa 1 (ALT-1: auto-migrate at boot).
#
# Runs `alembic upgrade head` against the live Postgres before exec'ing
# uvicorn. `depends_on: postgres condition: service_healthy` already
# orders this on docker-compose; the explicit pg_isready loop here covers
# the standalone-container case (running without compose) and protects
# against flaky DNS / brief Postgres bounces.
#
# `set -euo pipefail` makes any migration failure abort startup, so a
# broken migration cannot silently leave the API serving against stale
# schema. The advisory lock in alembic/env.py prevents concurrent
# migrations from racing when multiple containers boot simultaneously.
set -euo pipefail

# ----- Postgres readiness probe (M-9) ---------------------------------------
# Resolve POSTGRES_HOST / POSTGRES_PORT from the env (set by docker-compose).
PG_HOST="${POSTGRES_HOST:-postgres}"
PG_PORT="${POSTGRES_PORT:-5432}"
PG_USER="${POSTGRES_USER:-transcription}"
PG_DB="${POSTGRES_DB:-transcription_api}"
PG_TIMEOUT_SECONDS="${PG_READY_TIMEOUT:-60}"

echo "[entrypoint] waiting for postgres at ${PG_HOST}:${PG_PORT} (up to ${PG_TIMEOUT_SECONDS}s)"
deadline=$(( $(date +%s) + PG_TIMEOUT_SECONDS ))
until pg_isready -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" -t 2 >/dev/null 2>&1; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "[entrypoint] FAIL: postgres unreachable at ${PG_HOST}:${PG_PORT} after ${PG_TIMEOUT_SECONDS}s" >&2
        echo "[entrypoint] check: POSTGRES_HOST/POSTGRES_PORT env vars and depends_on healthcheck in compose" >&2
        exit 1
    fi
    sleep 2
done
echo "[entrypoint] postgres reachable"

# ----- Migrations ------------------------------------------------------------
echo "[entrypoint] running alembic upgrade head"
alembic upgrade head

echo "[entrypoint] starting uvicorn"
exec uvicorn transcription_api.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --log-config /app/src/transcription_api/logging.json
