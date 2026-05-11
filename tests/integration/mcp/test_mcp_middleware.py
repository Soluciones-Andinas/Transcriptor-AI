"""Bearer middleware on the MCP sub-app.

Spec: SPEC-capa4-mcp-v1
Covers:
- AC-8 — Every request to ``/mcp/...`` carries an
  ``Authorization: Bearer <plaintext>`` header. The middleware
  validates against ``mcp_bearers.token_hash`` (re-using the Capa 2
  ``verify_bearer`` semantics with bypass_scoping for the lookup) and
  returns 401 with a JSON body whose ``error_code`` discriminates:
  - ``MCP_BEARER_INVALID`` for missing / malformed header / unknown
    token / token whose row was never created.
  - ``MCP_BEARER_REVOKED`` for tokens whose row exists but has
    ``revoked_at IS NOT NULL``.
- AC-14 — A successful auth (valid + non-revoked bearer) bumps
  ``mcp_bearers.last_used_at`` to ``clock_timestamp()`` so operators
  can audit bearer activity from the DB.

These tests exercise the middleware via raw POSTs (no MCP client
SDK round-trip): the JSON-RPC handshake POST is enough to traverse
the auth layer; we assert on response status + JSON body shape and
on the DB row's ``last_used_at``. Going via raw HTTP keeps the test
contract resilient to MCP client SDK API changes and reduces the
fixture surface to: a Postgres testcontainer + a seeded bearer row.

All tests skip on CPU dev boxes without Docker (testcontainers
requires the daemon — same auto-skip as Capa 2 auth integration tests).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.factories import make_bearer, make_user
from transcription_api.auth.mcp_bearer import generate_bearer

pytestmark = pytest.mark.requires_docker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _initialize_payload() -> dict:
    """Minimal JSON-RPC initialize body that the SDK accepts as a handshake.

    The exact protocol_version is not asserted in these middleware tests —
    we only care that the request reaches (or is blocked at) the auth
    layer, NOT that the SDK responds initialize successfully. A non-401
    status code is the signal that the bearer is valid.
    """
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0"},
        },
    }


async def _post_mcp(headers: dict[str, str] | None = None):
    from transcription_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        # MCP Streamable-HTTP rejects requests without the protocol-version
        # negotiation header upfront, but the bearer middleware sits BEFORE
        # the SDK; if auth fails we bail before reaching that layer.
        merged = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        if headers:
            merged.update(headers)
        return await client.post("/mcp/", json=_initialize_payload(), headers=merged)


def _error_code(resp) -> str | None:
    """Extract ``detail.error_code`` from a JSON 401/403 body."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return detail.get("error_code")
    return None


# ---------------------------------------------------------------------------
# AC-8 — bearer auth
# ---------------------------------------------------------------------------
async def test_mcp_no_bearer_returns_401_invalid():
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-8 — Given no Authorization header, When POST /mcp,
    Then 401 with error_code=MCP_BEARER_INVALID. The middleware
    short-circuits before the SDK is reached.
    """
    resp = await _post_mcp(headers=None)

    assert resp.status_code == 401, resp.text
    assert _error_code(resp) == "MCP_BEARER_INVALID"


async def test_mcp_malformed_authorization_returns_401_invalid():
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-8 — Given Authorization that does NOT start with
    ``Bearer `` (case-insensitive), When POST /mcp, Then 401 with
    error_code=MCP_BEARER_INVALID. Same code as missing header — both
    fail the parse step.
    """
    resp = await _post_mcp(headers={"authorization": "Basic dXNlcjpwYXNz"})

    assert resp.status_code == 401, resp.text
    assert _error_code(resp) == "MCP_BEARER_INVALID"


async def test_mcp_nonexistent_bearer_returns_401_invalid(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-8 — Given a well-formed Bearer header whose plaintext
    has no row in mcp_bearers, When POST /mcp, Then 401 with
    error_code=MCP_BEARER_INVALID.
    """
    resp = await _post_mcp(
        headers={"authorization": "Bearer plaintext_never_generated"}
    )

    assert resp.status_code == 401, resp.text
    assert _error_code(resp) == "MCP_BEARER_INVALID"


async def test_mcp_revoked_bearer_returns_401_revoked(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-8 — Given a Bearer whose row exists but is revoked
    (revoked_at IS NOT NULL), When POST /mcp, Then 401 with
    error_code=MCP_BEARER_REVOKED. Distinct from INVALID so operators
    can tell the user "your bearer was revoked, regenerate a new one"
    rather than the generic "not a valid bearer".
    """
    user = await make_user(session, email="rev-mcp@x")
    plaintext, token_hash = generate_bearer()
    await make_bearer(
        session,
        user_id=user.id,
        token_hash=token_hash,
        revoked_at=datetime.now(tz=timezone.utc),
    )
    await session.commit()

    resp = await _post_mcp(headers={"authorization": f"Bearer {plaintext}"})

    assert resp.status_code == 401, resp.text
    assert _error_code(resp) == "MCP_BEARER_REVOKED"


@pytest.mark.skip(
    reason=(
        "D-081 follow-up: this happy-path test reaches FastMCP's "
        "StreamableHTTPSessionManager.handle_request which needs a live anyio "
        "TaskGroup on session_manager._task_group. The production lifespan "
        "supplies it via session_manager.run(); tests bypass the lifespan and "
        "can't open a real TaskGroup from a pytest-asyncio fixture (the "
        "setup/teardown phases run in different tasks, breaking anyio's "
        "same-task entry/exit invariant). Pre-D-081 this test was green by "
        "accident — the public URL was /mcp/mcp and POST /mcp/ returned 404 "
        "before reaching the handler; the assertion `status != 401` was "
        "trivially true. The happy path is now covered by the rig E2E "
        "(2026-05-08 deploy validated bearer auth + last_used_at bump in "
        "real traffic). Reinstate when we wire asgi-lifespan into the test "
        "harness or migrate to a real ASGI test server with lifespan support."
    )
)
async def test_mcp_valid_bearer_passes_middleware_and_bumps_last_used_at(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-8 + AC-14 — Given a valid (non-revoked) Bearer, When
    POST /mcp, Then the request traverses the bearer middleware (status
    is anything except 401 — the SDK may respond 200, 400, 406; what
    matters is that the middleware did NOT block) AND
    ``mcp_bearers.last_used_at`` is bumped to a non-NULL value.
    """
    from transcription_api.db.models import McpBearer

    user = await make_user(session, email="ok-mcp@x")
    plaintext, token_hash = generate_bearer()
    bearer = await make_bearer(
        session, user_id=user.id, token_hash=token_hash
    )
    await session.commit()
    assert bearer.last_used_at is None, "fresh bearer must have last_used_at NULL"

    resp = await _post_mcp(headers={"authorization": f"Bearer {plaintext}"})

    assert resp.status_code != 401, (
        f"valid bearer was rejected by middleware; body={resp.text!r}"
    )

    refreshed = (
        await session.execute(select(McpBearer).where(McpBearer.id == bearer.id))
    ).scalar_one()
    await session.refresh(refreshed)
    assert refreshed.last_used_at is not None, (
        "AC-14: successful auth must bump last_used_at"
    )


# ---------------------------------------------------------------------------
# G11.4 — AC-14 best-effort bump failure tolerated
# ---------------------------------------------------------------------------
@pytest.mark.skip(
    reason=(
        "D-081 follow-up: same TaskGroup limitation as "
        "test_mcp_valid_bearer_passes_middleware_and_bumps_last_used_at. "
        "The H-6 tolerance contract (bump failure must not cause 401) is "
        "covered by middleware unit tests that don't touch the SDK handler."
    )
)
async def test_mcp_valid_bearer_passes_when_last_used_at_bump_fails(
    session, monkeypatch
):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-14 + H-6 (G11 review-fix) — Given a valid bearer, When
    the best-effort ``last_used_at`` UPDATE fails (simulating a DB
    hiccup), Then the request still authenticates successfully — the
    middleware must NOT 401 just because the bump failed. Guards the
    tolerance contract: a flaky bump path cannot lock users out.
    """
    from unittest.mock import AsyncMock

    user = await make_user(session, email="bump-fail@x")
    plaintext, token_hash = generate_bearer()
    await make_bearer(session, user_id=user.id, token_hash=token_hash)
    await session.commit()

    monkeypatch.setattr(
        "transcription_api.mcp.middleware._bump_last_used_at",
        AsyncMock(side_effect=RuntimeError("simulated DB hiccup")),
    )

    resp = await _post_mcp(headers={"authorization": f"Bearer {plaintext}"})

    assert resp.status_code != 401, (
        "AC-14 H-6: a bump failure must not reject the request; "
        f"body={resp.text!r}"
    )
