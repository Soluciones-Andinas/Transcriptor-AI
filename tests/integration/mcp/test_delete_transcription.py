"""``delete_transcription`` tool — RF-MCP-09.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 5 task 5.2):
- AC-11 — Owner soft-deletes their own row: ``deleted_at`` is set
  on the Transcription AND cascade-set on every linked Image.
  Idempotency: a second call surfaces ``TRANSCRIPTION_NOT_FOUND``
  (the row is now soft-deleted; the WHERE-deleted_at-IS-NULL
  filter excludes it).
- Cross-user — User B deletes Alice's id -> NOT_FOUND. SAME body
  as truly-unknown id (no existence leak).
- ``INVALID_PARAMETER`` — transcription_id not a parseable UUID.

Tests run unit-style; ``requires_docker`` for the testcontainer.
"""
from __future__ import annotations

import secrets
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

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
# AC-11 — soft delete + cascade
# ---------------------------------------------------------------------------
async def test_delete_own_marks_deleted_at_and_returns_ok(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-11 — Given owner calls delete_transcription on their
    own row, When delete completes, Then Transcription.deleted_at is
    NOT NULL and the tool returns ``{ok: True}``.
    """
    from transcription_api.db.models import Transcription
    from transcription_api.mcp.tools.transcription import delete_transcription

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"del-{secrets.token_hex(2)}"
    )
    row = await make_transcription(
        session, user_id=user.id, audio_hash="del-" + "0" * 60
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        result = await delete_transcription(transcription_id=str(row.id))
    finally:
        reset()

    assert result == {"ok": True}

    # Re-fetch with bypass_scoping (we want to see the soft-deleted row).
    from transcription_api.db.scoping import bypass_scoping

    with bypass_scoping(session):
        refreshed = (
            await session.execute(
                select(Transcription).where(Transcription.id == row.id)
            )
        ).scalar_one()
    await session.refresh(refreshed)
    assert refreshed.deleted_at is not None


async def test_delete_cascades_soft_delete_to_images(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-11 cascade — Images linked to the deleted
    transcription must also flip to ``deleted_at IS NOT NULL`` so
    ``get_transcription`` doesn't return zombie images for the
    (now soft-deleted) row, and ``list_my_images``-style future
    queries respect the same per-user soft-delete contract.
    """
    from tests.factories import make_image
    from transcription_api.db.models import Image
    from transcription_api.db.scoping import bypass_scoping
    from transcription_api.mcp.tools.transcription import delete_transcription

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"cas-{secrets.token_hex(2)}"
    )
    row = await make_transcription(
        session, user_id=user.id, audio_hash="cas-" + "0" * 60
    )
    await make_image(session, transcription_id=row.id, user_id=user.id)
    await make_image(session, transcription_id=row.id, user_id=user.id)
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        await delete_transcription(transcription_id=str(row.id))
    finally:
        reset()

    with bypass_scoping(session):
        rows = (
            await session.execute(
                select(Image).where(Image.transcription_id == row.id)
            )
        ).scalars().all()
    for img in rows:
        await session.refresh(img)
        assert img.deleted_at is not None, (
            f"image {img.id} not cascade-soft-deleted"
        )


async def test_delete_idempotent_second_call_returns_not_found(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-11 idempotency — A second delete on the same id
    surfaces ``TRANSCRIPTION_NOT_FOUND`` (the WHERE-deleted_at-IS-NULL
    filter excludes the now-soft-deleted row from the UPDATE). Same
    body as cross-user / unknown — no leak about the row's history.
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.tools.transcription import delete_transcription

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"idem-{secrets.token_hex(2)}"
    )
    row = await make_transcription(
        session, user_id=user.id, audio_hash="idem-" + "0" * 59
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        # First delete succeeds.
        await delete_transcription(transcription_id=str(row.id))
        # Second delete -> NOT_FOUND.
        with pytest.raises(McpError) as exc:
            await delete_transcription(transcription_id=str(row.id))
        assert _is_tool_error(exc.value, "TRANSCRIPTION_NOT_FOUND")
    finally:
        reset()


# ---------------------------------------------------------------------------
# G11.6 — idempotent delete preserves first deleted_at
# ---------------------------------------------------------------------------
async def test_delete_idempotent_preserves_first_deleted_at(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-11 idempotency strengthened (G11 review-fix) — Given a
    transcription deleted at ``T0``, When a second delete on the same id
    runs (returning NOT_FOUND), Then ``transcriptions.deleted_at`` MUST
    still equal ``T0``. Strengthens the prior NOT_FOUND check by pinning
    the row contract: the second UPDATE filters by ``deleted_at IS NULL``,
    matches zero rows, and does not overwrite the original timestamp.
    Without this guarantee, a malicious caller could rotate the
    ``deleted_at`` value on a tombstoned row.
    """
    from mcp.shared.exceptions import McpError
    from sqlalchemy import text

    from transcription_api.mcp.tools.transcription import delete_transcription

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"idem-preserve-{secrets.token_hex(2)}"
    )
    row = await make_transcription(
        session,
        user_id=user.id,
        audio_hash="idemp-" + "0" * 58,
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        await delete_transcription(transcription_id=str(row.id))
    finally:
        reset()

    first_deleted_at = (
        await session.execute(
            text("SELECT deleted_at FROM transcriptions WHERE id = :tid"),
            {"tid": row.id},
        )
    ).scalar_one()
    assert first_deleted_at is not None

    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await delete_transcription(transcription_id=str(row.id))
        assert _is_tool_error(exc.value, "TRANSCRIPTION_NOT_FOUND")
    finally:
        reset()

    second_deleted_at = (
        await session.execute(
            text("SELECT deleted_at FROM transcriptions WHERE id = :tid"),
            {"tid": row.id},
        )
    ).scalar_one()
    assert second_deleted_at == first_deleted_at, (
        "deleted_at must NOT change on a second (rejected) delete"
    )


# ---------------------------------------------------------------------------
# Cross-user
# ---------------------------------------------------------------------------
async def test_delete_other_user_returns_not_found(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-11 + ADR-014/015 — User B requests delete on
    Alice's id -> NOT_FOUND (listener filters Alice's row out of B's
    UPDATE). Alice's row stays untouched (deleted_at IS NULL).
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.db.models import Transcription
    from transcription_api.db.scoping import bypass_scoping
    from transcription_api.mcp.tools.transcription import delete_transcription

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
            await delete_transcription(transcription_id=str(alice_row.id))
        assert _is_tool_error(exc.value, "TRANSCRIPTION_NOT_FOUND")
    finally:
        reset()

    # Alice's row must NOT have been touched.
    with bypass_scoping(session):
        refreshed = (
            await session.execute(
                select(Transcription).where(Transcription.id == alice_row.id)
            )
        ).scalar_one()
    await session.refresh(refreshed)
    assert refreshed.deleted_at is None, (
        "cross-user delete leaked through the listener"
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
async def test_delete_invalid_uuid_returns_invalid_parameter(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_PARAMETER — transcription_id not a
    parseable UUID.
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.tools.transcription import delete_transcription

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"bad-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await delete_transcription(transcription_id="not-a-uuid")
        assert _is_tool_error(exc.value, "INVALID_PARAMETER")
    finally:
        reset()


async def test_delete_unknown_id_returns_not_found(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-11 — Random UUID with no matching row -> SAME
    NOT_FOUND body as cross-user / soft-deleted.
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.tools.transcription import delete_transcription

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"ghost-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await delete_transcription(transcription_id=str(uuid4()))
        assert _is_tool_error(exc.value, "TRANSCRIPTION_NOT_FOUND")
    finally:
        reset()


