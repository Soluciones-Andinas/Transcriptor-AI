"""DB session lifecycle for MCP tool/resource handlers.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 1):
- ADR-014/015 — Each tool handler opens its own ``AsyncSession`` via
  this context manager, which arms ``db.info["user_id"]`` so the
  per-user scoping listener AND-injects ``WHERE user_id = X`` on every
  ORM query that targets a per-user model. Without this seam every
  tool would have to repeat the arm/disarm boilerplate.

Why a fresh session per tool call (vs FastAPI's per-request session):
FastMCP does not use FastAPI ``Depends``. The handler runs inside the
ASGI sub-app's own event loop and has no access to the parent
FastAPI's request-scoped session. Opening a session here keeps the
isolation per-handler explicit, with commit on the success path and
rollback on any exception.

Tools (Batch 2+) use this manager as::

    async with mcp_request_session(get_current_user_id()) as db:
        ...  # naive ORM queries are auto-scoped via the listener
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import async_session_factory


@asynccontextmanager
async def mcp_request_session(user_id: UUID) -> AsyncIterator[AsyncSession]:
    """Yield an ``AsyncSession`` armed with ``user_id`` for ADR-014/015.

    Commit on the happy path so the handler's writes land before the
    JSON-RPC response leaves the wire. Rollback on any exception —
    matches the FastAPI ``get_session`` dependency contract from
    Capa 1, transposed to the MCP execution model.
    """
    session: AsyncSession = async_session_factory()
    session.info["user_id"] = user_id
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        # Defense-in-depth: even though async_session_factory() returns
        # a fresh session per call, the matching disarm here keeps the
        # `session.info` slate empty in case a future refactor pools
        # sessions (mirrors CR-4 from Capa 2's get_session teardown).
        session.info.pop("user_id", None)
        session.info.pop("scoping_bypass", None)
        await session.close()
