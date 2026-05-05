"""Smoke test for the auth router wiring.

Spec: SPEC-capa2-auth-msentra-v1
Plan: docs/sesiones/2026-05-05-capa2-auth-plan.md (T4)
Confirms `app.include_router(auth_router)` works and the /auth/_ping
placeholder is reachable. Real endpoints land in Batches 2-5.
"""
from __future__ import annotations

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient


pytestmark = pytest.mark.requires_docker  # lifespan binds DB engine; needs DB


async def test_auth_ping_returns_ok():
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: T4 — auth router is wired into the app and reachable.
    """
    from transcription_api.main import app

    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            r = await client.get("/auth/_ping")
            assert r.status_code == 200
            assert r.json() == {"ok": True}
