"""Integration tests for POST /auth/regenerate-mcp-token.

Spec: SPEC-capa2-auth-msentra-v1
Plan: docs/sesiones/2026-05-05-capa2-auth-plan.md (T16)
Covers:
- AC-16 (T16): regenerate revokes old + emits new (one-shot plaintext in response).
- ERR-4 (T16): partial UNIQUE constraint catches concurrent regenerate;
  retry-once produces last-writer-wins semantics.
"""
from __future__ import annotations

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

pytestmark = pytest.mark.requires_docker


@pytest.fixture
async def client():
    from transcription_api.main import app
    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as c:
            yield c


async def _login_as(session, *, email: str = "alice@sandinas.test"):
    """Seed user + active bearer; return (user, bearer, session_cookie_value)."""
    from tests.factories import make_bearer, make_user
    from transcription_api.auth.session import create_session_token

    user = await make_user(session, email=email)
    bearer = await make_bearer(session, user_id=user.id, name="initial")
    await session.commit()
    token = create_session_token(
        user_id=user.id, ms_oid=user.microsoft_oid, email=user.email,
    )
    return user, bearer, token


# ---------------------------------------------------------------------------
# AC-16 / T16 — regenerate revokes old + issues new
# ---------------------------------------------------------------------------
async def test_regenerate_revokes_old_and_issues_new(client, session):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-16 — POST /auth/regenerate-mcp-token con cookie valida →
    200 con nuevo plaintext + bearer.id; el bearer viejo queda con
    revoked_at NOT NULL; el nuevo queda activo.
    """
    from transcription_api.auth.mcp_bearer import hash_bearer
    from transcription_api.db.models import McpBearer

    user, old_bearer, session_token = await _login_as(session, email="alice@x")
    old_bearer_id = old_bearer.id
    old_token_hash = old_bearer.token_hash

    r = await client.post(
        "/auth/regenerate-mcp-token", cookies={"session": session_token},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert "bearer" in body
    new_plaintext = body["bearer"]["plaintext"]
    assert new_plaintext, "response must contain new plaintext"
    assert body["bearer"]["id"] != str(old_bearer_id)
    assert body["bearer"]["name"] == "regenerated"

    # DB state: old bearer revoked, new bearer active, hashes match.
    old_row = (
        await session.execute(select(McpBearer).where(McpBearer.id == old_bearer_id))
    ).scalar_one()
    await session.refresh(old_row)
    assert old_row.revoked_at is not None
    assert old_row.token_hash == old_token_hash

    new_row = (
        await session.execute(
            select(McpBearer).where(McpBearer.user_id == user.id, McpBearer.revoked_at.is_(None))
        )
    ).scalar_one()
    assert new_row.token_hash == hash_bearer(new_plaintext)
    assert new_row.id != old_bearer_id

    # Cross-check: verify_bearer with the OLD plaintext returns None
    # (would need the actual plaintext though — old_bearer was created via
    # factory with a random hash, so we can't reverse it). We assert
    # via revoked_at instead.


# ---------------------------------------------------------------------------
# AC-16 / T16 — unauthenticated
# ---------------------------------------------------------------------------
async def test_regenerate_unauthenticated_returns_401(client, session):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-16 (negative) — sin cookie session → 401.
    """
    r = await client.post("/auth/regenerate-mcp-token")
    assert r.status_code == 401
    assert r.json()["detail"]["error_code"] == "AUTH_NOT_AUTHENTICATED"


# ---------------------------------------------------------------------------
# ERR-4 / T16 — race protection via partial UNIQUE
# ---------------------------------------------------------------------------
async def test_regenerate_handles_double_regen_via_retry(client, session):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: ERR-4 — dos POST /auth/regenerate-mcp-token CONCURRENTES por
    el mismo user. Ambos devuelven 200, los plaintexts difieren, y al final
    hay exactamente UN bearer activo en DB (last-writer-wins).

    H-3: la versión anterior secuencializaba las requests, lo cual no
    ejercía la lógica de retry-on-IntegrityError. asyncio.gather mete las
    dos POSTs en paralelo: ambas pegan a la DB casi simultáneamente, una
    de las dos pierde la UNIQUE parcial `uq_mcp_bearers_active_per_user`,
    rollback + retry, y eventualmente las dos terminan con 200.
    """
    import asyncio

    from transcription_api.auth.mcp_bearer import hash_bearer
    from transcription_api.db.models import McpBearer

    user, _, session_token = await _login_as(session, email="bob@x")

    r1, r2 = await asyncio.gather(
        client.post("/auth/regenerate-mcp-token", cookies={"session": session_token}),
        client.post("/auth/regenerate-mcp-token", cookies={"session": session_token}),
    )

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["bearer"]["plaintext"] != r2.json()["bearer"]["plaintext"]

    # Final DB state: exactly one active bearer per user (the partial UNIQUE
    # constraint guarantees it; the test confirms our retry logic doesn't
    # leave duplicate active rows).
    n_active = (
        await session.execute(
            select(func.count())
            .select_from(McpBearer)
            .where(McpBearer.user_id == user.id)
            .where(McpBearer.revoked_at.is_(None))
        )
    ).scalar_one()
    assert n_active == 1, (
        f"expected exactly 1 active bearer post-race; got {n_active}"
    )

    # Total bearers for user = 3 (initial + 2 regenerated).
    n_total = (
        await session.execute(
            select(func.count()).select_from(McpBearer).where(McpBearer.user_id == user.id)
        )
    ).scalar_one()
    assert n_total == 3

    # Whichever request was last-to-commit owns the active bearer.
    active = (
        await session.execute(
            select(McpBearer)
            .where(McpBearer.user_id == user.id)
            .where(McpBearer.revoked_at.is_(None))
        )
    ).scalar_one()
    plaintexts = {r1.json()["bearer"]["plaintext"], r2.json()["bearer"]["plaintext"]}
    assert active.token_hash in {hash_bearer(p) for p in plaintexts}, (
        "active bearer must correspond to one of the two race winners"
    )
