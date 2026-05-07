"""``lookup_owned_or_404`` collapses (cross-user, unknown, soft-deleted) -> 404.

Spec: SPEC-capa4-mcp-v1
Covers (G4 — review-fixes Tier 1):
- Privacy invariant — five MCP call sites previously copy-pasted the
  same SELECT-or-404 pattern. Centralizing the lookup in one helper
  forces every call site through identical semantics: cross-user,
  unknown id, and soft-deleted all collapse to the SAME error code +
  message (no existence leak across causes per ADR-015 fail-closed).

Unit-level: the helper takes an ``AsyncSession`` and a model class.
The session is mocked here because the contract under test is
"call the listener-armed SELECT, raise 404 on miss" — independent
of whether the SELECT really hits a Postgres testcontainer.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.shared.exceptions import McpError


class _FakeModel:
    """Stand-in for a per-user ORM model.

    Attributes are SQL column markers, not real Column objects; the
    helper only needs them to assemble a ``WHERE id = ...`` clause
    and (when ``soft_delete=True``) an ``AND deleted_at IS NULL``
    predicate. The MagicMock-backed AsyncSession does not actually
    parse the statement, so the markers can be plain attributes.
    """

    id = "id_col"
    deleted_at = "del_col"


@pytest.mark.asyncio
async def test_lookup_returns_row_when_found() -> None:
    """Happy path — listener-armed SELECT hits, helper returns the row."""
    from transcription_api.mcp.lookup import lookup_owned_or_404

    db = MagicMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: "row_fake")
    )

    out = await lookup_owned_or_404(
        db,
        _FakeModel,
        "uuid",
        error_code="TRANSCRIPTION_NOT_FOUND",
        error_message="transcription not found",
    )
    assert out == "row_fake"


@pytest.mark.asyncio
async def test_lookup_raises_404_when_not_found() -> None:
    """Miss path — None row collapses to McpError with the supplied code."""
    from transcription_api.mcp.lookup import lookup_owned_or_404

    db = MagicMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: None)
    )

    with pytest.raises(McpError) as exc:
        await lookup_owned_or_404(
            db,
            _FakeModel,
            "uuid",
            error_code="TRANSCRIPTION_NOT_FOUND",
            error_message="transcription not found",
        )
    assert exc.value.error.data["error_code"] == "TRANSCRIPTION_NOT_FOUND"
    assert exc.value.error.data["http_status"] == 404
