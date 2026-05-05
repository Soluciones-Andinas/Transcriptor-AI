"""Integration tests for the MCP bearer dependency `get_current_user_mcp`.

Spec: SPEC-capa2-auth-msentra-v1
Plan: docs/sesiones/2026-05-05-capa2-auth-plan.md (T18, T19)
Covers:
- AC-17 (T18): dependency parses Authorization Bearer + lookup + activates
  scoping. Parametrized over: no header, malformed, missing token, revoked,
  nonexistent, valid.
- AC-18 (T19): cross-user isolation invariant — user A's bearer can only see
  A's transcriptions; user B's bearer only sees B's. Validates the full chain
  HTTP → middleware → session.info["user_id"] → ADR-014 listener → WHERE clause.

Test routes are registered at module level under `/_test_mcp_*` paths. They
use the production `app` instance because the lifespan binds the engine and
the scoping listener is global. Production never references these paths.
"""
from __future__ import annotations

import pytest
from asgi_lifespan import LifespanManager
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.requires_docker


# ---------------------------------------------------------------------------
# Test routes — registered once at import, depend on production app.
# ---------------------------------------------------------------------------
def _register_test_routes_once() -> None:
    """Idempotently attach `/_test_mcp_*` routes to the production app.

    Returns immediately if already registered. Marked as test artifacts via
    the `_test_` prefix so they're easy to grep + exclude from production
    OpenAPI exposure (Capa 6 will use `include_in_schema=False`).
    """
    from transcription_api.auth.dependencies import get_current_user_mcp
    from transcription_api.db import get_session
    from transcription_api.db.models import Transcription, User
    from transcription_api.main import app

    if any(getattr(r, "path", None) == "/_test_mcp_protected" for r in app.routes):
        return

    @app.get("/_test_mcp_protected", include_in_schema=False)
    async def _protected(
        user: User = Depends(get_current_user_mcp),
    ) -> dict:
        """Returns the authenticated user's id; existence proves the dependency
        validates the bearer."""
        return {"user_id": str(user.id), "email": user.email}

    @app.get("/_test_mcp_user_data", include_in_schema=False)
    async def _user_data(
        user: User = Depends(get_current_user_mcp),
        db: AsyncSession = Depends(get_session),
    ) -> dict:
        """Naive `select(Transcription)` with NO explicit WHERE clause. The
        ADR-014 scoping listener should inject `WHERE user_id = X` because
        the dependency set `db.info["user_id"]`. AC-18 asserts cross-user
        isolation via this route."""
        rows = (await db.execute(select(Transcription))).scalars().all()
        return {"audio_hashes": sorted(r.audio_hash for r in rows)}


_register_test_routes_once()


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


# ---------------------------------------------------------------------------
# AC-17 / T18 — bearer dependency: 6 parametrized scenarios
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("scenario,expected_status", [
    ("no_header", 401),
    ("malformed_scheme", 401),
    ("empty_bearer", 401),
    ("nonexistent_bearer", 401),
    ("revoked_bearer", 401),
    ("valid_bearer", 200),
], ids=lambda x: x if isinstance(x, str) else None)
async def test_mcp_bearer_middleware(client, session, scenario, expected_status):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-17 — dependency parses + validates `Authorization: Bearer <plaintext>`.
    Six scenarios cover the failure surface + the success path.
    """
    from datetime import datetime, timezone

    from tests.factories import make_bearer, make_user
    from transcription_api.auth.mcp_bearer import generate_bearer

    headers: dict[str, str] = {}

    if scenario == "no_header":
        pass  # no Authorization
    elif scenario == "malformed_scheme":
        headers["authorization"] = "Basic dXNlcjpwYXNz"  # Basic, not Bearer
    elif scenario == "empty_bearer":
        headers["authorization"] = "Bearer  "  # Bearer with empty token
    elif scenario == "nonexistent_bearer":
        # A well-formed bearer header but the token doesn't exist in DB.
        headers["authorization"] = "Bearer plaintext_that_was_never_generated"
    elif scenario == "revoked_bearer":
        user = await make_user(session, email=f"rev-{scenario}@x")
        plaintext, token_hash = generate_bearer()
        await make_bearer(
            session, user_id=user.id, token_hash=token_hash,
            revoked_at=datetime.now(tz=timezone.utc),
        )
        await session.commit()
        headers["authorization"] = f"Bearer {plaintext}"
    elif scenario == "valid_bearer":
        user = await make_user(session, email=f"val-{scenario}@x")
        plaintext, token_hash = generate_bearer()
        await make_bearer(session, user_id=user.id, token_hash=token_hash)
        await session.commit()
        headers["authorization"] = f"Bearer {plaintext}"

    r = await client.get("/_test_mcp_protected", headers=headers)
    assert r.status_code == expected_status, r.text

    if expected_status == 200:
        body = r.json()
        assert body["user_id"] == str(user.id)
        assert body["email"] == user.email
    else:
        assert r.json()["detail"]["error_code"] == "AUTH_NOT_AUTHENTICATED"


async def test_mcp_bearer_bumps_last_used_at(client, session):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-17 (extension) — verify_bearer side-effect: successful auth
    bumps `mcp_bearers.last_used_at` so the operator can audit usage.
    """
    from tests.factories import make_bearer, make_user
    from transcription_api.auth.mcp_bearer import generate_bearer
    from transcription_api.db.models import McpBearer

    user = await make_user(session, email="bumper@x")
    plaintext, token_hash = generate_bearer()
    bearer = await make_bearer(session, user_id=user.id, token_hash=token_hash)
    await session.commit()
    assert bearer.last_used_at is None, "fresh bearer has last_used_at NULL"

    r = await client.get(
        "/_test_mcp_protected", headers={"authorization": f"Bearer {plaintext}"},
    )
    assert r.status_code == 200

    # Refresh bearer state from DB; last_used_at should now be set.
    refreshed = (
        await session.execute(select(McpBearer).where(McpBearer.id == bearer.id))
    ).scalar_one()
    await session.refresh(refreshed)
    assert refreshed.last_used_at is not None


# ---------------------------------------------------------------------------
# AC-18 / T19 — cross-user isolation activates ADR-014 scoping
# ---------------------------------------------------------------------------
async def test_mcp_bearer_activates_per_user_scoping(client, session):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-18 — the full chain (HTTP → get_current_user_mcp →
    session.info["user_id"] → do_orm_execute listener → WHERE filter)
    isolates per-user data. User A's bearer reading `select(Transcription)`
    sees only A's transcriptions; user B's bearer sees only B's.

    This is the LOAD-BEARING invariant for Capa 6. If it breaks, MCP tools
    leak between tenants regardless of which query the developer writes.
    """
    from tests.factories import make_bearer, make_transcription, make_user
    from transcription_api.auth.mcp_bearer import generate_bearer

    # Setup: 2 users, 2 bearers, 2 transcriptions (one per user).
    alice = await make_user(session, email="alice-iso@x")
    bob = await make_user(session, email="bob-iso@x")

    alice_pt, alice_hash = generate_bearer()
    bob_pt, bob_hash = generate_bearer()
    await make_bearer(session, user_id=alice.id, token_hash=alice_hash)
    await make_bearer(session, user_id=bob.id, token_hash=bob_hash)

    await make_transcription(session, user_id=alice.id, audio_hash="hash-alice-only")
    await make_transcription(session, user_id=bob.id, audio_hash="hash-bob-only")
    await session.commit()

    # Alice's bearer → sees ONLY hash-alice-only.
    r_alice = await client.get(
        "/_test_mcp_user_data", headers={"authorization": f"Bearer {alice_pt}"},
    )
    assert r_alice.status_code == 200, r_alice.text
    assert r_alice.json()["audio_hashes"] == ["hash-alice-only"], (
        f"alice's bearer leaked cross-user data: {r_alice.json()}"
    )

    # Bob's bearer → sees ONLY hash-bob-only.
    r_bob = await client.get(
        "/_test_mcp_user_data", headers={"authorization": f"Bearer {bob_pt}"},
    )
    assert r_bob.status_code == 200, r_bob.text
    assert r_bob.json()["audio_hashes"] == ["hash-bob-only"], (
        f"bob's bearer leaked cross-user data: {r_bob.json()}"
    )

    # No bearer → 401, no data exposed.
    r_anon = await client.get("/_test_mcp_user_data")
    assert r_anon.status_code == 401
