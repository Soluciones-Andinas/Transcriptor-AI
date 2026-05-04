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
def _docker_available() -> bool:
    """Probe whether `docker compose` will work locally."""
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


@pytest.mark.skipif(not _docker_available(), reason="docker daemon required")
def test_compose_up_health_smoke():
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-15 (review fix CR-2) — full `docker compose up --build`
    brings up the stack with auto-migrations and `/health` reports
    `db_reachable: true`.

    Uses `docker-compose.cpu.yml` override to strip `gpus: all` so the test
    runs on Mac (no nvidia-container-toolkit) AND on the production rig.
    The CUDA Docker image still builds; we just don't reserve a GPU at
    runtime. Inside the container, /health reports `gpu_backend: cpu`,
    which is the right thing to assert here.

    Operators on the rig validate the FULL GPU path with
    `bash scripts/smoke-capa1.sh` (no override = `gpus: all` enforced).
    """
    import json
    import time

    repo_root = REPO_ROOT
    project_name = "transcription-api-e2e"
    compose_args = [
        "docker", "compose",
        "-p", project_name,
        "-f", str(repo_root / "docker-compose.yml"),
        "-f", str(repo_root / "docker-compose.cpu.yml"),
    ]

    # Pre-test cleanup — leftover state would defeat the smoke.
    subprocess.run(
        compose_args + ["down", "-v", "--remove-orphans"],
        cwd=str(repo_root),
        capture_output=True,
        timeout=60,
    )

    try:
        # Up + build. The CUDA base image (~5 GB) on a fresh host takes a while
        # to pull on residential connections; allow up to 60 minutes once.
        # Subsequent runs reuse the cached image and complete in ~30 seconds.
        up = subprocess.run(
            compose_args + ["up", "--build", "-d", "--wait"],
            cwd=str(repo_root),
            timeout=3600,
        )
        if up.returncode != 0:
            # Capture container logs to make CI failures debuggable.
            logs = subprocess.run(
                compose_args + ["logs", "--tail=100"],
                cwd=str(repo_root), capture_output=True, text=True, timeout=15,
            )
            pytest.fail(
                f"compose up exited {up.returncode}\n"
                f"--- container logs (tail 100) ---\n{logs.stdout}\n{logs.stderr}"
            )

        # Wait for /health to come back healthy. `--wait` already polls the
        # healthcheck, but the healthcheck itself only requires HTTP 200,
        # not db_reachable=true. Re-poll for the stronger assertion.
        deadline = time.time() + 120
        last_response = None
        while time.time() < deadline:
            try:
                result = subprocess.run(
                    ["curl", "-fsS", "http://localhost:8000/health"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    last_response = json.loads(result.stdout)
                    if last_response.get("db_reachable") is True:
                        break
            except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
                pass
            time.sleep(3)

        assert last_response is not None, "no successful /health response"
        assert last_response["db_reachable"] is True, (
            f"db_reachable was {last_response.get('db_reachable')}; full body: {last_response}"
        )
        # gpu_backend=cpu is the expected value under the cpu override.
        assert last_response["gpu_backend"] in {"cuda", "mps", "cpu"}
        assert last_response["status"] == "ok"

        # Verify alembic actually ran (not just that /health returns 200).
        alembic_check = subprocess.run(
            compose_args + ["exec", "-T", "postgres",
                            "psql", "-U", "transcription", "-d", "transcription_api",
                            "-tAc", "SELECT version_num FROM alembic_version"],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=10,
        )
        version = (alembic_check.stdout or "").strip()
        assert len(version) == 12, (
            f"alembic_version not at head: stdout={alembic_check.stdout!r} "
            f"stderr={alembic_check.stderr!r}"
        )
    finally:
        subprocess.run(
            compose_args + ["down", "-v", "--remove-orphans"],
            cwd=str(repo_root),
            capture_output=True,
            timeout=60,
        )
