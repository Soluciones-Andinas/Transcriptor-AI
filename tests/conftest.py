"""Shared pytest fixtures.

Spec: SPEC-capa1-postgres-orm-v1
- `pg_container`: ephemeral postgres:16-alpine via testcontainers (session-scoped).
- `migrated_db_url`: pg_container URL after `alembic upgrade head` (session-scoped).
- `engine` / `session`: per-test AsyncEngine + AsyncSession bound to the migrated DB.

ERR-4 honored: tests requiring Docker are auto-skipped if testcontainers can't
reach the daemon (e.g., dev machine without Docker running).
"""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator

import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Marker registration
# ---------------------------------------------------------------------------
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_docker: test requires a running Docker daemon (testcontainers).",
    )


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
    """Per-test AsyncSession with auto-rollback to keep tests isolated."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s
        await s.rollback()
