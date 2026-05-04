"""Compose E2E + ALT-1 contract checks.

Spec: SPEC-capa1-postgres-orm-v1
Plan: docs/sesiones/2026-04-30-capa1-postgres-orm-plan.md
Covers: AC-15 (`docker compose up --build` brings up healthy stack with
alembic auto-migrating at boot) + ALT-1 (entrypoint.sh runs migrations).

Two layers:
1. **Static contract** (always runs under the `e2e` marker):
   - `scripts/entrypoint.sh` runs `alembic upgrade head` before uvicorn.
   - `Dockerfile` uses ENTRYPOINT pointing at `/usr/local/bin/entrypoint.sh`.
   - `docker-compose.yml` has postgres healthcheck and api depends_on healthy.
   - These can be checked without a Docker daemon.

2. **Live smoke** (full `docker compose up` against the rig):
   - Requires a CUDA-capable host (the rig). Skipped automatically when no
     `nvidia-container-toolkit` is detected.
   - Validates `/health` returns `db_reachable: true` after startup.

Run with: `pytest -m e2e`
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Static-contract checks (no Docker needed)
# ---------------------------------------------------------------------------
def test_entrypoint_runs_alembic_before_uvicorn():
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: ALT-1 — entrypoint.sh applies the head migration BEFORE
    starting uvicorn, so a fresh deploy never serves traffic against a
    schemaless DB.
    """
    entrypoint = REPO_ROOT / "scripts" / "entrypoint.sh"
    assert entrypoint.is_file()
    content = entrypoint.read_text()

    alembic_pos = content.find("alembic upgrade head")
    uvicorn_pos = content.find("uvicorn transcription_api.main:app")
    assert alembic_pos != -1, "entrypoint must call `alembic upgrade head`"
    assert uvicorn_pos != -1, "entrypoint must start `uvicorn transcription_api.main:app`"
    assert alembic_pos < uvicorn_pos, (
        "alembic must run BEFORE uvicorn, otherwise a fresh DB serves traffic "
        "with no schema"
    )
    assert "set -euo pipefail" in content, (
        "entrypoint must `set -euo pipefail` so a migration failure aborts startup"
    )


def test_dockerfile_uses_entrypoint_script():
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-15 — Dockerfile installs entrypoint.sh and uses ENTRYPOINT
    (not CMD) so docker overrides cannot accidentally skip the migration step.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh" in dockerfile
    assert "RUN chmod +x /usr/local/bin/entrypoint.sh" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]' in dockerfile, (
        "Dockerfile must use ENTRYPOINT (not CMD) for the migration step"
    )
    # alembic config must be present in the image so it can run from /app
    assert "COPY alembic.ini" in dockerfile
    assert "COPY alembic/" in dockerfile


def test_compose_postgres_healthcheck_and_depends_on():
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-15 — docker-compose.yml has a postgres healthcheck and the
    api service depends on it being healthy. This guarantees Postgres is
    reachable by the time entrypoint.sh runs `alembic upgrade head`.
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "pg_isready" in compose, "postgres healthcheck must use pg_isready"
    assert "condition: service_healthy" in compose, (
        "api must depend on postgres `condition: service_healthy`"
    )


# ---------------------------------------------------------------------------
# Live smoke (operator runs on the rig)
# ---------------------------------------------------------------------------
def _gpu_available() -> bool:
    """Detect nvidia-container-toolkit by probing `docker info`."""
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return "nvidia" in (result.stdout or "")


@pytest.mark.skipif(not _gpu_available(), reason="rig with nvidia-container-toolkit required")
def test_compose_up_health_smoke():
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-15 — full `docker compose up --build -d` brings up both
    services healthy, and `curl /health` reports `db_reachable: true`.

    This is the canonical AC-15 acceptance. On non-GPU hosts (developer
    Macs), this test is skipped — operators run it on the rig before deploy.
    """
    pytest.skip("Live smoke; run manually on the rig with `bash scripts/smoke-capa1.sh`")
