"""``get_user_info`` tool — RF-MCP-* user-info surface.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 5 task 5.3):
- Tool returns ``{user_id, email, display_name, bearer_id}`` for the
  caller. The two ContextVars armed by the bearer middleware drive
  this entirely — no input args, the user is whoever the bearer
  identifies.

Note on listener scoping: ``User`` does NOT carry a ``user_id``
column (it IS the per-user root entity), so the ADR-014/015 listener
does NOT classify it as scoped — see ``db.scoping._scoped_models``,
which only includes mappers with a ``user_id`` column. The plan
suggested ``bypass_scoping(db)`` defensively; verified empirically
the listener leaves User SELECTs alone, so the bypass is unnecessary.
"""
from __future__ import annotations

import secrets
from uuid import UUID

import pytest

from tests.factories import make_bearer, make_user
from transcription_api.auth.mcp_bearer import generate_bearer

pytestmark = pytest.mark.requires_docker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _seed_user_with_bearer(session, *, email_suffix: str):
    user = await make_user(session, email=f"u-{email_suffix}@x")
    plain, token_hash = generate_bearer()
    bearer = await make_bearer(session, user_id=user.id, token_hash=token_hash)
    return user, bearer


def _arm_context(user_id: UUID, bearer_id: UUID):
    from transcription_api.mcp.middleware import (
        _current_bearer_id,
        _current_user_id,
    )

    user_token = _current_user_id.set(user_id)
    bearer_token = _current_bearer_id.set(bearer_id)

    def _reset():
        _current_user_id.reset(user_token)
        _current_bearer_id.reset(bearer_token)

    return _reset


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
async def test_get_user_info_returns_caller_identity(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: get_user_info — Returns the bearer-identified user's
    ``user_id``, ``email``, ``display_name``, and the active
    ``bearer_id``. No input args; the ContextVars are the source of
    truth.
    """
    from transcription_api.mcp.tools.transcription import get_user_info

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"info-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        result = await get_user_info()
    finally:
        reset()

    assert result["user_id"] == str(user.id)
    assert result["email"] == user.email
    assert result["display_name"] == user.display_name
    assert result["bearer_id"] == str(bearer.id)


async def test_get_user_info_two_users_get_distinct_payloads(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: get_user_info — The result tracks the ContextVar
    setting per-call; calling once as A and once as B (in series)
    returns each user's own identity. Validates the ContextVar
    plumbing wires through correctly without sticky state.
    """
    from transcription_api.mcp.tools.transcription import get_user_info

    alice, alice_bearer = await _seed_user_with_bearer(
        session, email_suffix=f"alice-{secrets.token_hex(2)}"
    )
    bob, bob_bearer = await _seed_user_with_bearer(
        session, email_suffix=f"bob-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset_a = _arm_context(alice.id, alice_bearer.id)
    try:
        result_a = await get_user_info()
    finally:
        reset_a()
    reset_b = _arm_context(bob.id, bob_bearer.id)
    try:
        result_b = await get_user_info()
    finally:
        reset_b()

    assert result_a["user_id"] == str(alice.id)
    assert result_a["email"] == alice.email
    assert result_b["user_id"] == str(bob.id)
    assert result_b["email"] == bob.email
    assert result_a["bearer_id"] != result_b["bearer_id"]
