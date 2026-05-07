"""``list_my_transcriptions`` tool — RF-MCP-04.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 4 task 4.1):
- AC-3 — User A's call returns ONLY A's rows; user B's rows are
  filtered by the ADR-014/015 listener (no leak even when the
  caller forgets the WHERE clause).
- Pagination — ``limit`` clamped to 100; ``offset`` honored; ``total``
  reflects the WHERE-deleted_at-IS-NULL filter (soft-deleted rows
  excluded from both list and total).
- Sort — ``created_at_desc`` (default), ``created_at_asc``,
  ``duration_desc`` are accepted; anything else raises
  ``INVALID_PARAMETER``.

Tests run unit-style: import tool function, arm
``_current_user_id`` / ``_current_bearer_id`` ContextVars, hit the
real testcontainer Postgres. ``requires_docker`` auto-skip on CPU
dev box.
"""
from __future__ import annotations

import secrets
from uuid import UUID

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
# AC-3 — cross-user isolation
# ---------------------------------------------------------------------------
async def test_list_returns_only_callers_transcriptions(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-3 — Given user A has 3 transcriptions and user B has
    2, When A calls list_my_transcriptions, Then result.items has
    exactly 3 entries (all owned by A) and result.total == 3. The
    listener AND-injects user_id automatically; the tool's SELECT
    has NO explicit WHERE user_id and still gets the right answer.
    """
    from transcription_api.mcp.tools.transcription import list_my_transcriptions

    alice, alice_bearer = await _seed_user_with_bearer(
        session, email_suffix=f"alice-{secrets.token_hex(2)}"
    )
    bob, _ = await _seed_user_with_bearer(
        session, email_suffix=f"bob-{secrets.token_hex(2)}"
    )

    for i in range(3):
        await make_transcription(
            session, user_id=alice.id, audio_hash=f"alice-hash-{i:02d}-" + "0" * 47
        )
    for i in range(2):
        await make_transcription(
            session, user_id=bob.id, audio_hash=f"bob-hash-{i:02d}-" + "0" * 49
        )
    await session.commit()

    reset = _arm_context(alice.id, alice_bearer.id)
    try:
        result = await list_my_transcriptions()
    finally:
        reset()

    assert result["total"] == 3
    assert len(result["items"]) == 3
    # No row from Bob leaked through.
    audio_hashes = {item["audio_hash"] for item in result["items"]}
    assert all(h.startswith("alice-hash-") for h in audio_hashes)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
async def test_list_respects_limit_and_offset(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: pagination — Given 5 rows for the user, When called
    with limit=2 offset=2, Then items has 2 entries (rows 3-4 in
    sort order) and total reflects the unpaged count (5).
    """
    from transcription_api.mcp.tools.transcription import list_my_transcriptions

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"page-{secrets.token_hex(2)}"
    )
    for i in range(5):
        await make_transcription(
            session, user_id=user.id, audio_hash=f"page-{i:02d}-" + "0" * 56
        )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        result = await list_my_transcriptions(limit=2, offset=2)
    finally:
        reset()

    assert result["total"] == 5
    assert result["limit"] == 2
    assert result["offset"] == 2
    assert len(result["items"]) == 2


async def test_list_clamps_oversized_limit_to_100(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: pagination — limit > 100 silently clamps to 100. The
    response's ``limit`` field reflects the clamped value so the
    client knows what was actually applied.
    """
    from transcription_api.mcp.tools.transcription import list_my_transcriptions

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"clamp-{secrets.token_hex(2)}"
    )
    await make_transcription(session, user_id=user.id)
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        result = await list_my_transcriptions(limit=999)
    finally:
        reset()

    assert result["limit"] == 100


# ---------------------------------------------------------------------------
# Soft-delete filter
# ---------------------------------------------------------------------------
async def test_list_excludes_soft_deleted(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: list filters ``deleted_at IS NULL`` — soft-deleted
    rows do not appear in items NOR contribute to total.
    """
    from datetime import datetime, timezone

    from sqlalchemy import update

    from transcription_api.db.models import Transcription
    from transcription_api.mcp.tools.transcription import list_my_transcriptions

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"soft-{secrets.token_hex(2)}"
    )
    keep = await make_transcription(
        session, user_id=user.id, audio_hash="keep-" + "0" * 60
    )
    deleted = await make_transcription(
        session, user_id=user.id, audio_hash="dele-" + "0" * 60
    )
    await session.execute(
        update(Transcription)
        .where(Transcription.id == deleted.id)
        .values(deleted_at=datetime.now(timezone.utc))
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        result = await list_my_transcriptions()
    finally:
        reset()

    assert result["total"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["id"] == str(keep.id)


# ---------------------------------------------------------------------------
# Sort validation
# ---------------------------------------------------------------------------
async def test_list_rejects_unknown_sort(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_PARAMETER — sort outside the
    documented enum is rejected before hitting the DB.
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.tools.transcription import list_my_transcriptions

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"sort-bad-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await list_my_transcriptions(sort="random_column_desc")
        assert _is_tool_error(exc.value, "INVALID_PARAMETER")
    finally:
        reset()


# ---------------------------------------------------------------------------
# G5 — pagination params hardening
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"limit": -1, "offset": 0},
        {"limit": 0, "offset": 0},
        {"limit": 10, "offset": -1},
        {"limit": 999, "offset": 0},  # > _LIST_LIMIT_MAX*2
    ],
)
async def test_list_invalid_params_raise(session, bad_kwargs):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_PARAMETER — out-of-band pagination values
    raise instead of being silently clamped. Drops the silent-clamp on
    clearly-bug inputs (negative / zero limit, negative offset, absurd
    over-cap) so a buggy caller gets a signal rather than degraded
    pagination.
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.tools.transcription import list_my_transcriptions

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"badparams-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await list_my_transcriptions(**bad_kwargs)
        assert _is_tool_error(exc.value, "INVALID_PARAMETER")
    finally:
        reset()
