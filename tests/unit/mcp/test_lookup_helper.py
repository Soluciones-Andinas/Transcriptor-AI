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
from uuid import uuid4

import pytest
from mcp.shared.exceptions import McpError


@pytest.mark.asyncio
async def test_lookup_returns_row_when_found() -> None:
    """Happy path — listener-armed SELECT hits, helper returns the row."""
    from transcription_api.db.models import Transcription
    from transcription_api.mcp.lookup import lookup_owned_or_404

    sentinel = object()  # the helper only forwards what scalar_one_or_none yields
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: sentinel)
    )

    out = await lookup_owned_or_404(
        db,
        Transcription,
        uuid4(),
        error_code="TRANSCRIPTION_NOT_FOUND",
        error_message="transcription not found",
    )
    assert out is sentinel


@pytest.mark.asyncio
async def test_lookup_raises_404_when_not_found() -> None:
    """Miss path — None row collapses to McpError with the supplied code."""
    from transcription_api.db.models import Transcription
    from transcription_api.mcp.lookup import lookup_owned_or_404

    db = MagicMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: None)
    )

    with pytest.raises(McpError) as exc:
        await lookup_owned_or_404(
            db,
            Transcription,
            uuid4(),
            error_code="TRANSCRIPTION_NOT_FOUND",
            error_message="transcription not found",
        )
    assert exc.value.error.data["error_code"] == "TRANSCRIPTION_NOT_FOUND"
    assert exc.value.error.data["http_status"] == 404


@pytest.mark.asyncio
async def test_lookup_excludes_soft_deleted_when_model_has_deleted_at() -> None:
    """Soft-delete branch — ``deleted_at IS NULL`` predicate gets added.

    Confirmed indirectly by inspecting the rendered SQL: when the model
    has a ``deleted_at`` column and ``soft_delete=True`` (the default),
    the WHERE clause must include the IS NULL predicate.
    """
    from sqlalchemy.dialects import postgresql

    from transcription_api.db.models import Transcription
    from transcription_api.mcp.lookup import lookup_owned_or_404

    captured: dict = {}

    async def _capture(stmt):
        captured["sql"] = str(
            stmt.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        return MagicMock(scalar_one_or_none=lambda: "row")

    db = MagicMock()
    db.execute = _capture

    await lookup_owned_or_404(
        db,
        Transcription,
        uuid4(),
        error_code="TRANSCRIPTION_NOT_FOUND",
        error_message="transcription not found",
    )
    assert "deleted_at IS NULL" in captured["sql"], captured["sql"]
