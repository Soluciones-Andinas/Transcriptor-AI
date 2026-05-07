"""``request_upload_url`` tool — RF-MCP-01.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 2):
- AC-1 — Issues an ephemeral upload bearer (one-shot, ~32-char URL-safe)
  + signed URL pointing at ``POST /api/upload`` (or ``/api/upload-image``).
  The DB stores only ``SHA-256(plaintext)`` (D-044) so a leaked DB
  cannot replay uploads. Plaintext is shown to the MCP client ONCE in
  the result and never persisted.
- AC-1 image-cross-user — Image uploads MUST reference an existing
  ``transcription_id`` owned by the caller. The listener fail-closed
  (ADR-014/015) handles this transparently: a SELECT for somebody
  else's transcription_id returns no row; the tool maps that to
  ``TRANSCRIPTION_NOT_FOUND`` (no existence leak).
- spec §4 — Per-kind validation surface emits typed errors:
  ``INVALID_PARAMETER`` for kind/size/mime/transcription_id misuse,
  ``FILE_TOO_LARGE`` when size exceeds the per-kind cap.

The tool reads ``current_user_id`` and ``current_bearer_id`` from the
ContextVars armed by the bearer middleware (Batch 1) — no extra DB
lookup needed for either. Persistence runs in a fresh
``mcp_request_session(user_id)`` so the listener AND-injects scope
on every query inside.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from ...config import settings
from ...db.models import Transcription, UploadSession
from ..errors import raise_tool_error
from ..lookup import lookup_owned_or_404
from ..middleware import get_current_bearer_id, get_current_user_id
from ..server import mcp_server
from ..session import mcp_request_session

# RF-MCP-01: image MIMEs we accept upfront. Audio doesn't list a MIME
# whitelist here because ffmpeg sniffs audio at normalize time (Capa 3).
_IMAGE_MIMES_OK = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)


@mcp_server.tool(name="request_upload_url")
async def request_upload_url(
    kind: str,
    file_size_bytes: int,
    mime_type: str | None = None,
    transcription_id: str | None = None,
) -> dict[str, Any]:
    """Issue an ephemeral upload URL + bearer for an audio or image upload.

    Args:
        kind: ``"audio"`` or ``"image"``. Audio is the entry point of
            ``start_transcription``; image attaches to an existing
            transcription owned by the caller.
        file_size_bytes: hint used for both the per-kind cap check and
            the ``+5%`` streaming cap on the upload endpoint.
        mime_type: required for ``kind="image"``; ignored for audio
            (ffmpeg sniffs the container at normalize time).
        transcription_id: required for ``kind="image"`` (UUID string);
            the listener verifies ownership.

    Returns:
        ``{"upload_url", "upload_id", "bearer", "expires_at"}``. Plaintext
        ``bearer`` is shown ONCE — the DB only persists its SHA-256.
    """
    # ------------------------------------------------------------------
    # Pure-input validation (no DB yet — fail fast, save a session open).
    # ------------------------------------------------------------------
    if kind not in ("audio", "image"):
        raise_tool_error(
            "INVALID_PARAMETER",
            f"kind must be 'audio' or 'image'; got {kind!r}",
            400,
        )

    if file_size_bytes <= 0:
        raise_tool_error(
            "INVALID_PARAMETER",
            f"file_size_bytes must be > 0; got {file_size_bytes}",
            400,
        )

    if kind == "audio":
        max_bytes = settings.max_upload_mb * 1024 * 1024
        max_mb = settings.max_upload_mb
    else:
        max_bytes = settings.max_image_upload_mb * 1024 * 1024
        max_mb = settings.max_image_upload_mb
        if mime_type is None:
            raise_tool_error(
                "INVALID_PARAMETER",
                "mime_type required for kind=image",
                400,
            )
        if mime_type not in _IMAGE_MIMES_OK:
            raise_tool_error(
                "INVALID_PARAMETER",
                f"mime_type {mime_type!r} not supported; "
                f"expected one of {sorted(_IMAGE_MIMES_OK)}",
                400,
            )
        if transcription_id is None:
            raise_tool_error(
                "INVALID_PARAMETER",
                "transcription_id required for kind=image",
                400,
            )

    if file_size_bytes > max_bytes:
        raise_tool_error(
            "FILE_TOO_LARGE",
            f"size {file_size_bytes} exceeds max {max_mb} MB for kind={kind}",
            413,
            max_mb=max_mb,
        )

    user_id = get_current_user_id()
    bearer_id = get_current_bearer_id()

    # ------------------------------------------------------------------
    # DB session — listener AND-injects user_id automatically because
    # mcp_request_session arms session.info["user_id"]. The
    # transcription_id ownership check therefore runs naturally scoped:
    # User B looking up A's id sees nothing -> TRANSCRIPTION_NOT_FOUND
    # (no existence leak per AC-1 image-cross-user).
    # ------------------------------------------------------------------
    async with mcp_request_session(user_id) as db:
        tid_uuid: UUID | None = None
        if kind == "image":
            try:
                tid_uuid = UUID(transcription_id)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise_tool_error(
                    "INVALID_PARAMETER",
                    f"transcription_id is not a valid UUID: {transcription_id!r}",
                    400,
                )
            # Cross-user / unknown / soft-deleted all collapse to
            # TRANSCRIPTION_NOT_FOUND via the shared helper (G4 review-fix).
            await lookup_owned_or_404(
                db,
                Transcription,
                tid_uuid,
                error_code="TRANSCRIPTION_NOT_FOUND",
                error_message="transcription not found",
            )

        upload_id = uuid4()
        nonce = secrets.token_urlsafe(32)
        bearer_for_upload = secrets.token_urlsafe(32)
        upload_bearer_hash = sha256(
            bearer_for_upload.encode("ascii")
        ).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.upload_session_ttl_seconds
        )

        row = UploadSession(
            id=upload_id,
            user_id=user_id,
            bearer_id=bearer_id,
            nonce=nonce,
            upload_bearer_hash=upload_bearer_hash,
            kind=kind,
            transcription_id=tid_uuid,
            expected_size_bytes=file_size_bytes,
            expected_mime_type=mime_type,
            expires_at=expires_at,
        )
        db.add(row)
        await db.flush()

    path = "/api/upload" if kind == "audio" else "/api/upload-image"
    upload_url = f"{settings.public_base_url}{path}?session={nonce}"

    return {
        "upload_url": upload_url,
        "upload_id": str(upload_id),
        "bearer": bearer_for_upload,
        "expires_at": expires_at.isoformat(),
    }
