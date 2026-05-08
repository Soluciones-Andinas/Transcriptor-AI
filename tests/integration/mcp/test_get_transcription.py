"""``get_transcription`` tool — RF-MCP-06.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 4 task 4.3):
- AC-5 — Owner reads their full transcription dict; cross-user
  request returns ``TRANSCRIPTION_NOT_FOUND`` with the SAME body
  shape as a truly-unknown id (no existence leak).
- Soft-deleted rows return ``TRANSCRIPTION_NOT_FOUND`` (same code as
  cross-user / unknown — also no leak about the row's history).
- ``INVALID_PARAMETER`` — transcription_id is not a valid UUID.

Tests run unit-style; ``requires_docker`` for the testcontainer.
"""
from __future__ import annotations

import secrets
from uuid import UUID, uuid4

import pytest

from tests.factories import make_bearer, make_transcription, make_user
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


def _is_tool_error(exc, expected_code: str) -> bool:
    from mcp.shared.exceptions import McpError

    if not isinstance(exc, McpError):
        return False
    data = getattr(exc.error, "data", None) if hasattr(exc, "error") else None
    return isinstance(data, dict) and data.get("error_code") == expected_code


# ---------------------------------------------------------------------------
# AC-5 — owner reads full result
# ---------------------------------------------------------------------------
async def test_get_transcription_own_returns_full_payload(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-5 — Owner reads their transcription -> full dict
    with id + audio_hash + language + duration_seconds + num_speakers
    + text_content + segments + metadata + images (empty list when
    none attached).
    """
    from transcription_api.mcp.tools.transcription import get_transcription

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"own-{secrets.token_hex(2)}"
    )
    row = await make_transcription(
        session,
        user_id=user.id,
        text="contenido completo de la transcripcion",
        audio_hash="own-" + "0" * 60,
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        result = await get_transcription(transcription_id=str(row.id))
    finally:
        reset()

    assert result["id"] == str(row.id)
    assert result["audio_hash"] == row.audio_hash
    assert result["language"] == row.language
    assert result["num_speakers"] == row.num_speakers
    assert "text_content" in result
    assert "segments" in result
    assert "metadata" in result
    assert "images" in result
    assert result["images"] == []  # no images attached


# ---------------------------------------------------------------------------
# AC-5 — cross-user returns NOT_FOUND with same body
# ---------------------------------------------------------------------------
async def test_get_transcription_other_user_returns_not_found(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-5 + ADR-014/015 — User B requests A's
    transcription_id -> TRANSCRIPTION_NOT_FOUND. Same code as
    truly-unknown id (no existence leak).
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.tools.transcription import get_transcription

    alice, _ = await _seed_user_with_bearer(
        session, email_suffix=f"alice-{secrets.token_hex(2)}"
    )
    alice_row = await make_transcription(
        session, user_id=alice.id, audio_hash="alice-" + "0" * 58
    )
    bob, bob_bearer = await _seed_user_with_bearer(
        session, email_suffix=f"bob-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset = _arm_context(bob.id, bob_bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await get_transcription(transcription_id=str(alice_row.id))
        assert _is_tool_error(exc.value, "TRANSCRIPTION_NOT_FOUND")
    finally:
        reset()


async def test_get_transcription_unknown_id_returns_not_found(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-5 — Random UUID with no matching row -> same
    TRANSCRIPTION_NOT_FOUND body as cross-user.
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.tools.transcription import get_transcription

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"ghost-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await get_transcription(transcription_id=str(uuid4()))
        assert _is_tool_error(exc.value, "TRANSCRIPTION_NOT_FOUND")
    finally:
        reset()


# ---------------------------------------------------------------------------
# Soft-delete
# ---------------------------------------------------------------------------
async def test_get_transcription_soft_deleted_returns_not_found(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-5 — Owner requests their own row but it has been
    soft-deleted (deleted_at IS NOT NULL) -> TRANSCRIPTION_NOT_FOUND.
    Same body as never-existed (no leak about the row's history).
    """
    from datetime import datetime, timezone

    from mcp.shared.exceptions import McpError
    from sqlalchemy import update

    from transcription_api.db.models import Transcription
    from transcription_api.mcp.tools.transcription import get_transcription

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"soft-{secrets.token_hex(2)}"
    )
    row = await make_transcription(
        session, user_id=user.id, audio_hash="soft-" + "0" * 60
    )
    await session.execute(
        update(Transcription)
        .where(Transcription.id == row.id)
        .values(deleted_at=datetime.now(timezone.utc))
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await get_transcription(transcription_id=str(row.id))
        assert _is_tool_error(exc.value, "TRANSCRIPTION_NOT_FOUND")
    finally:
        reset()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
async def test_get_transcription_invalid_uuid_returns_invalid_parameter(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_PARAMETER — transcription_id is not a
    parseable UUID.
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.tools.transcription import get_transcription

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"bad-uuid-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await get_transcription(transcription_id="not-a-uuid-string")
        assert _is_tool_error(exc.value, "INVALID_PARAMETER")
    finally:
        reset()
