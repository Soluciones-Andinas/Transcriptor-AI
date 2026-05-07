"""MCP resources — RF-MCP-06 + RF-IMG resource surface.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 5 task 5.1):
- AC-6 — ``transcription://{id}`` returns the same dict as
  ``get_transcription(id)`` (the resource is a thin delegation;
  cross-user / unknown / soft-deleted preserve the
  no-existence-leak invariant).
- AC-7 — ``transcription://{tid}/images/{iid}`` returns the binary
  payload of the image file. Cross-user / unknown /
  not-attached-to-tid -> ``IMAGE_NOT_FOUND``. Bytes are read off
  the filesystem at ``<DATA_DIR>/blobs/<user_id>/<tid>/<iid>.<ext>``
  via the row's stored ``file_path``.

Resource handlers live here (separate module from tools) because
FastMCP registers them with ``@mcp_server.resource(uri_template)``,
a different decorator from ``@mcp_server.tool``. Importing this
module from ``mcp/__init__.py`` triggers the registrations BEFORE
``streamable_http_app()`` snapshots the registry.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from ..db.models import Image
from .errors import raise_tool_error
from .middleware import get_current_user_id
from .server import mcp_server
from .session import mcp_request_session
from .tools.transcription import get_transcription


@mcp_server.resource("transcription://{transcription_id}")
async def transcription_resource(transcription_id: str) -> dict[str, Any]:
    """``transcription://{id}`` resource — thin reuse of ``get_transcription``.

    Keeps the contract in one place: any change to the dict shape
    or the cross-user error mapping in ``get_transcription`` flows
    through here automatically.
    """
    return await get_transcription(transcription_id=transcription_id)


@mcp_server.resource("transcription://{transcription_id}/images/{image_id}")
async def image_resource(transcription_id: str, image_id: str) -> bytes:
    """``transcription://{tid}/images/{iid}`` resource — binary image bytes.

    Reads the file at ``Image.file_path`` (set when the image upload
    landed in Batch 2; the resource is byte-passthrough). Cross-user
    or unknown id -> ``IMAGE_NOT_FOUND`` (listener filters
    ``Image.user_id``; same body across causes — no existence leak).
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
    try:
        iid = UUID(image_id)
    except (TypeError, ValueError):
        raise_tool_error(
            "INVALID_PARAMETER",
            f"image_id is not a valid UUID: {image_id!r}",
            400,
        )

    async with mcp_request_session(user_id) as db:
        # Listener AND-injects user_id (Image carries it denormalized
        # from Capa 1). Cross-user / unknown / soft-deleted /
        # not-attached-to-tid all yield None and collapse to
        # IMAGE_NOT_FOUND (no existence leak).
        img = (
            await db.execute(
                select(Image).where(
                    Image.id == iid,
                    Image.transcription_id == tid,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if img is None:
            raise_tool_error(
                "IMAGE_NOT_FOUND",
                "image not found",
                404,
            )
        file_path = img.file_path

    # File read happens after the session closes — keeps the DB
    # connection short-lived. If the path is missing on disk, treat
    # as IMAGE_NOT_FOUND (the row is orphan; same body for the same
    # client-visible reason).
    path = Path(file_path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise_tool_error(
            "IMAGE_NOT_FOUND",
            "image file missing on disk",
            404,
        )
