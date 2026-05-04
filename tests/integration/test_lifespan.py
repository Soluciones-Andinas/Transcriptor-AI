"""FastAPI lifespan owns the AsyncEngine.

Spec: SPEC-capa1-postgres-orm-v1
Plan: docs/sesiones/2026-04-30-capa1-postgres-orm-plan.md
Covers: AC-8 (lifespan creates engine on startup, disposes on shutdown).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

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


async def test_lifespan_disposes_engine_on_shutdown(monkeypatch):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-8 — at shutdown, `engine.dispose()` is awaited (releases pool).

    We swap the module-level singleton with an AsyncMock(spec=AsyncEngine)
    BEFORE lifespan runs, then assert dispose() was awaited at shutdown.
    """
    fake_engine = AsyncMock(spec=AsyncEngine)
    # Provide a usable .url attribute since lifespan logs `engine.url.host`.
    fake_engine.url = type("U", (), {"host": "test"})()
    monkeypatch.setattr("transcription_api.db.session.engine", fake_engine)
    monkeypatch.setattr("transcription_api.db.engine", fake_engine)

    from transcription_api.main import app

    async with LifespanManager(app):
        pass  # enter -> startup, exit -> shutdown calls dispose()

    fake_engine.dispose.assert_awaited_once()
