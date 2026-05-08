"""``get_transcription`` tool — RF-MCP-06.

Spec: SPEC-capa4-mcp-v1
Covers (G6 — review-fixes Tier 2 split):
- AC-5 — full transcription dict by id; cross-user / unknown /
  soft-deleted all collapse to ``TRANSCRIPTION_NOT_FOUND`` (404)
  with identical body — privacy invariant via
  ``lookup_owned_or_404`` (G4 review-fix).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from ...db.models import Image, Transcription
from ..errors import raise_tool_error
from ..lookup import lookup_owned_or_404
from ..middleware import get_current_user_id
from ..serializers import serialize_full
from ..server import mcp_server
from ..session import mcp_request_session


@mcp_server.tool(name="get_transcription")
async def get_transcription(transcription_id: str) -> dict[str, Any]:
    """Full transcription dict for a single id (AC-5).

    Cross-user / unknown / soft-deleted all collapse to
    ``TRANSCRIPTION_NOT_FOUND`` (404) with identical body — no
    existence leak across the three causes.
    """
    user_id = get_current_user_id()

    try:
        tid = UUID(transcription_id)
    except (TypeError, ValueError):
        raise_tool_error(
            "INVALID_PARAMETER",
            f"transcription_id is not a valid UUID: {transcription_id!r}",
            400,
        )

    async with mcp_request_session(user_id) as db:
        # Listener AND-injects user_id; cross-user / unknown / soft-deleted
        # all collapse to TRANSCRIPTION_NOT_FOUND via the shared helper
        # (G4 review-fix — privacy invariant lives in lookup_owned_or_404).
        row = await lookup_owned_or_404(
            db,
            Transcription,
            tid,
            error_code="TRANSCRIPTION_NOT_FOUND",
            error_message="transcription not found",
        )

        # Fetch attached images (also user-scoped + soft-delete-filtered).
        # Image carries user_id denormalized so the listener applies.
        images = (
            await db.execute(
                select(Image).where(
                    Image.transcription_id == tid,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalars().all()

    return serialize_full(row, list(images))
