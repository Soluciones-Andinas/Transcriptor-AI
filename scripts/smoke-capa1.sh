#!/usr/bin/env bash
# Manual Capa 1 smoke — run on the rig (CUDA + nvidia-container-toolkit).
#
# Validates AC-15 + ALT-1: docker compose up --build brings up both
# services healthy and /health reports db_reachable: true.
#
# Usage:
#   cd transcription-api
#   bash scripts/smoke-capa1.sh
#
# Exit code 0 on success; non-zero on any failure.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "[smoke] cleaning prior state"
docker compose down -v >/dev/null 2>&1 || true

echo "[smoke] building and starting compose stack (postgres + transcription-api)"
docker compose up --build -d

echo "[smoke] waiting up to 90s for transcription-api healthcheck"
deadline=$(( $(date +%s) + 90 ))
while true; do
    health=$(docker inspect --format='{{json .State.Health.Status}}' transcription-api 2>/dev/null || echo '"missing"')
    if [ "$health" = '"healthy"' ]; then
        break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "[smoke] FAIL: transcription-api did not become healthy in 90s (last status: $health)"
        docker compose logs transcription-api | tail -50
        exit 1
    fi
    sleep 3
done

echo "[smoke] curling /health"
response=$(curl -fsS http://localhost:8000/health)
echo "[smoke] response: $response"

if ! echo "$response" | grep -q '"db_reachable":true'; then
    echo "[smoke] FAIL: /health does not report db_reachable=true"
    exit 1
fi

echo "[smoke] OK — Capa 1 stack healthy and DB reachable"

echo "[smoke] tearing down"
docker compose down -v >/dev/null
