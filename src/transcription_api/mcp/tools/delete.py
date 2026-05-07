"""``delete_transcription`` tool — RF-MCP-09.

Spec: SPEC-capa4-mcp-v1
Covers (G6 — review-fixes Tier 2 split):
- AC-11 — soft-delete the caller's transcription + cascade to its
  attached images. Idempotent: a second call's UPDATE filters out the
  now-soft-deleted row, rowcount stays zero, helper raises NOT_FOUND
  with the same body as cross-user / unknown (no existence leak).
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, update

from ...db.models import Image, Transcription
from ..errors import raise_tool_error
from ..middleware import get_current_user_id
from ..server import mcp_server
from ..session import mcp_request_session

logger = logging.getLogger("transcription_api.mcp.tools.delete")


@mcp_server.tool(name="delete_transcription")
async def delete_transcription(transcription_id: str) -> dict[str, Any]:
    """Soft-delete the caller's transcription + cascade to its images (AC-11).

    Soft-delete via ``deleted_at = now()`` so the row stays around for
    audit / undelete (Capa 5 may add a hard-purge job). Cascade applies
    to any non-deleted attached Image rows: keeps the per-user
    soft-delete contract symmetric (a deleted parent never has visible
    children).

    Idempotent by listener convention: a second call's UPDATE filters
    out the now-soft-deleted row (``WHERE deleted_at IS NULL``); zero
    rowcount -> NOT_FOUND with the same body as cross-user / unknown
    (no existence-leak across the three causes).
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
        # Listener AND-injects user_id; cross-user UPDATEs filter out
        # the foreign row, the rowcount stays zero, and we return
        # NOT_FOUND below — no existence leak.
        result = await db.execute(
            update(Transcription)
            .where(
                Transcription.id == tid,
                Transcription.deleted_at.is_(None),
            )
            .values(deleted_at=func.now())
        )
        if result.rowcount == 0:
            raise_tool_error(
                "TRANSCRIPTION_NOT_FOUND",
                "transcription not found",
                404,
            )

        # Cascade — keep the per-user soft-delete contract symmetric so
        # `get_transcription` of the deleted parent never returns its
        # (now-orphan) images. Image carries user_id denormalized so
        # the listener applies the same scoping here.
        cascade = await db.execute(
            update(Image)
            .where(
                Image.transcription_id == tid,
                Image.deleted_at.is_(None),
            )
            .values(deleted_at=func.now())
        )
        # G8.5 — surface the cascade size so an audit trail can spot a
        # transcription that ended up with abnormally many image rows
        # (e.g., a runaway attachment loop). 0 is the common case for
        # transcriptions that never had images attached.
        logger.info(
            "delete_cascade_images user_id=%s transcription_id=%s rowcount=%d",
            user_id,
            tid,
            cascade.rowcount,
        )

    return {"ok": True}
