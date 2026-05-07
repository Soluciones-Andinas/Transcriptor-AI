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

import asyncio
from collections.abc import AsyncGenerator, Generator

import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Cross-event-loop hygiene for module-level asyncio primitives
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_orchestrator_lock_per_test():
    """Reset the orchestrator's module-level ``asyncio.Lock`` before each test.

    ``pipeline.orchestrator._orchestrator_lock`` wraps an ``asyncio.Lock``
    that binds to the running event loop on first acquire. pytest-asyncio
    creates a fresh loop per function-scoped test, so the lock from test
    1's loop becomes unusable in test 2 with
    ``RuntimeError: <Lock locked> is bound to a different event loop``.

    The autouse fixture rebinds the inner ``asyncio.Lock`` per-test,
    preserving the outer ``_OrchestrationLock`` object identity (so test
    imports via ``from ..orchestrator import _orchestrator_lock`` keep
    working) but resetting the loop affinity. Cheap (~µs); applied to
    every test for safety, not just pipeline ones — auth callback tests
    transitively import the orchestrator (via ``api.transcriptions``)
    and hit the same trap if a prior pipeline test contaminated the lock.

    Robust to import order: if the orchestrator module hasn't loaded yet
    (rare; some pure-unit tests), the fixture no-ops.
    """
    try:
        from transcription_api.pipeline import orchestrator
    except ImportError:
        yield
        return

    lock_obj = orchestrator._orchestrator_lock
    lock_obj._lock = asyncio.Lock()
    lock_obj._owner_task = None
    yield


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
    """Per-test AsyncEngine pointed at the migrated testcontainer DB.

    Also overrides ``transcription_api.db.session.engine`` and
    ``async_session_factory`` so production code paths invoked directly
    by tests (e.g. MCP tools that use ``scoped_session(user_id)``) hit
    the testcontainer DB instead of the default ``POSTGRES_HOST=postgres``
    which does not resolve on CI runners. Latent since Capa 2: tests
    that invoke tools directly (no FastAPI client wrapper) used the
    production module's engine, which only happened to work when the
    dev box had ``POSTGRES_HOST`` pointing at the same testcontainer
    via docker-compose. CI without that wiring exposed the gap.
    """
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    eng = create_async_engine(migrated_db_url, future=True)
    factory = async_sessionmaker(bind=eng, expire_on_commit=False, class_=AsyncSession)

    # Patch production module attributes so `scoped_session` (and any
    # other call site reading these via name lookup at call time) uses
    # the testcontainer engine. Snapshot + restore for hermetic teardown.
    import transcription_api.db.session as _session_mod

    _orig_engine = _session_mod.engine
    _orig_factory = _session_mod.async_session_factory
    _session_mod.engine = eng
    _session_mod.async_session_factory = factory
    try:
        yield eng
    finally:
        _session_mod.engine = _orig_engine
        _session_mod.async_session_factory = _orig_factory
        await eng.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncGenerator:
    """Per-test AsyncSession with auto-rollback + post-test TRUNCATE.

    The fixture session is the test driver's lane (factories, verification
    queries) — NOT the lane the FastAPI request handlers use. We arm
    `scoping_bypass` once so test setup/assert queries on per-user models
    don't trip the fail-closed listener (CR-5). Real per-user scoping
    behavior is exercised through the FastAPI client + `get_session()`
    dependency, which produces a separate session armed with `user_id`
    by the auth middleware.

    **TRUNCATE on teardown** (Capa 4 CI G14 fix): rollback alone keeps
    the test driver lane clean, but production code paths exercised by
    integration tests (notably ``auth/routes.py::callback`` upsert under
    ``bypass_scoping``) commit through the FastAPI dependency session.
    Those commits survive the test-driver rollback. Without the
    TRUNCATE, the next test (or the next parametrize value) hits
    ``UniqueViolationError`` on hardcoded emails like
    ``alice@sandinas.test``. TRUNCATE CASCADE wipes every per-user
    table after each test, restoring isolation. Cost: ~5ms per test
    against an empty schema.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        s.info["scoping_bypass"] = True
        try:
            yield s
        finally:
            await s.rollback()

    # Wipe any rows committed via production code paths during the test.
    # CASCADE handles dependent tables; explicit list keeps intent clear.
    async with engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE users, oauth_tokens, mcp_bearers, transcriptions, "
            "images, upload_sessions RESTART IDENTITY CASCADE"
        ))
