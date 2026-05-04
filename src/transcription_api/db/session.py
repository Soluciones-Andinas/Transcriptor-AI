"""Async engine + session factory + FastAPI dependency.

Spec: SPEC-capa1-postgres-orm-v1, AC-7 (engine from settings) + AC-8 (lifespan).

The engine is created at module import time so it can be referenced as a
canonical singleton from `db.__init__` and re-bound to `app.state.engine`
inside the FastAPI lifespan. `create_async_engine` does NOT open a TCP
connection on construction; the pool stays cold until first `connect()`.

Note (ERR-2 — pool exhaustion): when all `db_pool_size + db_pool_max_overflow`
connections are in use, SQLAlchemy raises TimeoutError; the calling endpoint
should map this to HTTP 503 with `error_code: DB_POOL_EXHAUSTED`. With the
expected workload (~5 transcripciones/day) this is highly unlikely.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import settings


def _make_engine() -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_max_overflow,
        pool_pre_ping=True,
        future=True,
    )


engine: AsyncEngine = _make_engine()

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields one AsyncSession per request, with rollback on error."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
