"""Shared pytest fixtures.

Spec: SPEC-capa1-postgres-orm-v1
- `pg_container`: ephemeral postgres:16-alpine via testcontainers (session-scoped).
- `migrated_db_url`: pg_container URL after `alembic upgrade head` (session-scoped).
- `engine` / `session`: per-test AsyncEngine + AsyncSession bound to the migrated DB.

ERR-4 honored: tests requiring Docker are auto-skipped if testcontainers can't
reach the daemon (e.g., dev machine without Docker running).

`requires_gpu` marker: tests that need a CUDA or MPS accelerator are auto-skipped
on CPU-only machines. Resolution uses `transcription_api.gpu.detect_accelerator`,
the same helper `/health` uses, so test environment matches runtime exactly.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Marker-driven auto-skip
# ---------------------------------------------------------------------------
def _docker_daemon_reachable() -> bool:
    """Return True if `docker info` succeeds within a few seconds.

    Used to auto-skip ``requires_docker``-marked tests on dev machines that
    don't have Docker installed or running, mirroring the existing
    ``requires_gpu`` skip path. Conservative: any failure (binary missing,
    timeout, non-zero exit) treats the daemon as unreachable.
    """
    import subprocess

    try:
        result = subprocess.run(  # noqa: S603,S607
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _ffmpeg_available() -> bool:
    """Return True if both ``ffmpeg`` and ``ffprobe`` resolve on PATH.

    Used to auto-skip ``requires_ffmpeg``-marked tests on machines without
    ffmpeg installed. The Capa 3 pipeline shells out to both binaries
    (normalize via ffmpeg, duration via ffprobe), so missing either is
    enough to treat the marker as unsupported.
    """
    import shutil

    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``requires_*`` tests when the host can't run them."""
    from transcription_api.gpu import detect_accelerator

    accel = detect_accelerator()
    skip_no_gpu = (
        pytest.mark.skip(
            reason=f"no accelerator detected (backend={accel.backend}); "
            "requires CUDA (NVIDIA) or MPS (Apple Silicon)"
        )
        if not accel.available
        else None
    )

    docker_ok = _docker_daemon_reachable()
    skip_no_docker = (
        pytest.mark.skip(reason="docker daemon unreachable; install/start Docker to run")
        if not docker_ok
        else None
    )

    ffmpeg_ok = _ffmpeg_available()
    skip_no_ffmpeg = (
        pytest.mark.skip(reason="ffmpeg/ffprobe not on PATH; install ffmpeg to run")
        if not ffmpeg_ok
        else None
    )

    for item in items:
        if skip_no_gpu and "requires_gpu" in item.keywords:
            item.add_marker(skip_no_gpu)
        if skip_no_docker and "requires_docker" in item.keywords:
            item.add_marker(skip_no_docker)
        if skip_no_ffmpeg and "requires_ffmpeg" in item.keywords:
            item.add_marker(skip_no_ffmpeg)


# ---------------------------------------------------------------------------
# Postgres testcontainer (session-scoped) + migrations applied once.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def pg_container() -> Generator[str, None, None]:
    """Spin up postgres:16-alpine; yield the asyncpg-formatted URL."""
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as e:
        pytest.skip(f"testcontainers not installed: {e}")

    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"docker not available for testcontainers: {e}")

    try:
        # testcontainers returns a psycopg2 URL by default; rewrite to asyncpg.
        url = container.get_connection_url()
        async_url = url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        yield async_url
    finally:
        container.stop()


@pytest.fixture(scope="session")
def migrated_db_url(pg_container: str) -> str:
    """Apply `alembic upgrade head` against pg_container; yield the same URL."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(repo_root / "alembic.ini"))
    # Alembic runs sync — convert asyncpg URL to psycopg.
    sync_url = pg_container.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    cfg.set_main_option("sqlalchemy.url", sync_url)
    # Ensure script_location is absolute (Config defaults to relative-to-cwd).
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    command.upgrade(cfg, "head")
    return pg_container


@pytest_asyncio.fixture
async def engine(migrated_db_url: str):
    """Per-test AsyncEngine pointed at the migrated testcontainer DB."""
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(migrated_db_url, future=True)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncGenerator:
    """Per-test AsyncSession with auto-rollback to keep tests isolated.

    The fixture session is the test driver's lane (factories, verification
    queries) — NOT the lane the FastAPI request handlers use. We arm
    `scoping_bypass` once so test setup/assert queries on per-user models
    don't trip the fail-closed listener (CR-5). Real per-user scoping
    behavior is exercised through the FastAPI client + `get_session()`
    dependency, which produces a separate session armed with `user_id`
    by the auth middleware.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        s.info["scoping_bypass"] = True
        yield s
        await s.rollback()
