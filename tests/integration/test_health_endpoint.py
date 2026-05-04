"""GET /health DB-reachability semantics.

Spec: SPEC-capa1-postgres-orm-v1
Plan: docs/sesiones/2026-04-30-capa1-postgres-orm-plan.md
Covers:
- AC-9 — `/health` reports `db_reachable: true` when SELECT 1 succeeds.
- ERR-1 — `/health` does NOT crash and returns 200 with `db_reachable: false`
  when the DB is unreachable. (Returning 200 prevents Docker's restart policy
  from looping the container while operators investigate.)
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError


pytestmark = pytest.mark.requires_docker


@asynccontextmanager
async def _client_with_engine(target_engine):
    """Open a LifespanManager(app), swap app.state.engine to `target_engine`,
    yield an AsyncClient bound to that app."""
    from transcription_api.main import app

    async with LifespanManager(app):
        app.state.engine = target_engine
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client


async def test_health_reports_db_reachable_true_when_db_up(engine):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-9 — with a reachable DB, /health returns 200 + db_reachable=true.
    """
    async with _client_with_engine(engine) as client:
        r = await client.get("/health")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["db_reachable"] is True
        assert body["status"] == "ok"


async def test_health_reports_db_reachable_false_when_db_down(engine, monkeypatch):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-9 + ERR-1 — when SELECT 1 raises OperationalError, /health
    must NOT crash; it returns 200 with db_reachable=false (degraded payload).
    """
    async def boom(*args, **kwargs):
        raise OperationalError("simulated connection refused", None, Exception())

    monkeypatch.setattr(
        "sqlalchemy.ext.asyncio.AsyncConnection.execute",
        boom,
    )
    async with _client_with_engine(engine) as client:
        r = await client.get("/health")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["db_reachable"] is False
