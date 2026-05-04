"""FastAPI lifespan owns the AsyncEngine.

Spec: SPEC-capa1-postgres-orm-v1
Plan: docs/sesiones/2026-04-30-capa1-postgres-orm-plan.md
Covers: AC-8 (lifespan creates engine on startup, disposes on shutdown).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from asgi_lifespan import LifespanManager
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_lifespan_sets_engine_on_app_state():
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-8 — after startup, `app.state.engine` is an AsyncEngine.
    """
    from transcription_api.main import app

    async with LifespanManager(app):
        assert hasattr(app.state, "engine"), "lifespan must assign app.state.engine"
        assert isinstance(app.state.engine, AsyncEngine)


async def test_lifespan_disposes_engine_on_shutdown():
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-8 — at shutdown, `engine.dispose()` is awaited (releases pool).
    """
    from transcription_api.main import app

    with patch(
        "transcription_api.db.session.engine.dispose",
        new=AsyncMock(),
    ) as mock_dispose:
        async with LifespanManager(app):
            pass  # enter and exit -> shutdown triggers dispose
        mock_dispose.assert_awaited_once()
