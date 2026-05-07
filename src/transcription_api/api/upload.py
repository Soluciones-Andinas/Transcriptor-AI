"""``POST /api/upload`` — RF-MCP-03 chunked-audio upload endpoint.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 2 task 2.4):
- AC-1 — Receives the binary requested via the MCP tool
  ``request_upload_url``. Auth uses the ephemeral bearer (validated
  against ``upload_sessions.upload_bearer_hash`` with
  ``hmac.compare_digest``); on success the file lands in
  ``<DATA_DIR>/uploads/<upload_id>/original.bin`` and the row's
  ``status`` flips to ``'uploaded'``.
- spec §4 ``MCP_BEARER_INVALID`` — missing header / wrong plaintext.
- spec §4 ``FILE_TOO_LARGE`` — bytes streamed exceed
  ``expected_size_bytes * 1.05``; partial file unlinked.
- AC-10 ``UPLOAD_SESSION_NOT_FOUND`` — unknown nonce OR expired
  session. Same error_code regardless of cause (no existence leak).
- spec §4 ``INVALID_PARAMETER`` — session.kind != 'audio'. Image
  upload uses a separate endpoint (RF-IMG; deferred).

Note: the image endpoint (``POST /api/upload-image``) is RF-IMG scope
and lands in a later batch. The audio endpoint here is the minimum
needed for the ``request_upload_url(audio) -> POST -> start_transcription``
chain to close end-to-end at Batch 3.
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    Query,
    UploadFile,
)
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..db.models import UploadSession
from ..db.scoping import bypass_scoping

logger = logging.getLogger("transcription_api.api.upload")

router = APIRouter(prefix="/api", tags=["uploads"])


def _error_resp(status: int, code: str, reason: str, **extra: Any) -> JSONResponse:
    """Canonical error body shape for the upload endpoints."""
    detail: dict[str, Any] = {"error_code": code, "reason": reason}
    detail.update(extra)
    return JSONResponse(status_code=status, content={"detail": detail})


@router.post("/upload")
async def upload_audio(
    session_nonce: str = Query(..., alias="session"),
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Chunked-upload endpoint for audio binaries (RF-MCP-03)."""

    # ------------------------------------------------------------------
    # 1. Bearer parse — fast-fail before any DB hit.
    # ------------------------------------------------------------------
    if not authorization or not authorization.lower().startswith("bearer "):
        return _error_resp(
            401, "MCP_BEARER_INVALID", "missing or malformed Authorization header"
        )
    plaintext = authorization[len("Bearer ") :].strip()
    if not plaintext:
        return _error_resp(
            401, "MCP_BEARER_INVALID", "empty bearer token"
        )

    # ------------------------------------------------------------------
    # 2. Lookup upload session by nonce (cross-user — listener bypass).
    #    The endpoint owner is identified by the bearer match below; we
    #    look up by nonce alone here, then verify the bearer hash.
    # ------------------------------------------------------------------
    with bypass_scoping(db):
        row = (
            await db.execute(
                select(UploadSession).where(
                    UploadSession.nonce == session_nonce,
                    UploadSession.status == "requested",
                )
            )
        ).scalar_one_or_none()

    # AC-10: unknown nonce OR expired session both return the same
    # 404 with UPLOAD_SESSION_NOT_FOUND (no existence leak about
    # whether the session ever existed).
    if row is None:
        return _error_resp(
            404, "UPLOAD_SESSION_NOT_FOUND", "session not found or already consumed"
        )
    # row.expires_at is timezone-aware (TIMESTAMPTZ); compare in UTC.
    if row.expires_at < datetime.now(timezone.utc):
        return _error_resp(
            404, "UPLOAD_SESSION_NOT_FOUND", "session expired"
        )

    # ------------------------------------------------------------------
    # 3. Kind match — this endpoint is audio-only.
    # ------------------------------------------------------------------
    if row.kind != "audio":
        return _error_resp(
            400,
            "INVALID_PARAMETER",
            f"endpoint expects kind='audio', session has kind={row.kind!r}",
        )

    # ------------------------------------------------------------------
    # 4. Bearer match — hmac.compare_digest is constant-time to thwart
    #    timing oracles on the sha256 hex comparison.
    # ------------------------------------------------------------------
    received_hash = sha256(plaintext.encode("ascii")).hexdigest()
    if not hmac.compare_digest(received_hash, row.upload_bearer_hash):
        return _error_resp(
            401, "MCP_BEARER_INVALID", "bearer hash mismatch"
        )

    # ------------------------------------------------------------------
    # 5. Stream the body to disk with the +5% margin cap. A cooperative
    #    client sends exactly expected_size_bytes; the margin protects
    #    against compression-frame edge cases without inviting abuse.
    # ------------------------------------------------------------------
    max_bytes = int(row.expected_size_bytes * 1.05)
    target_dir = settings.uploads_dir / str(row.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "original.bin"

    bytes_written = 0
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    fh.close()
                    target.unlink(missing_ok=True)
                    return _error_resp(
                        413,
                        "FILE_TOO_LARGE",
                        f"file exceeds expected_size_bytes ({row.expected_size_bytes}) "
                        "+ 5% margin",
                        max_bytes=max_bytes,
                    )
                fh.write(chunk)
    except Exception:
        # Best-effort cleanup of the partial write so disk doesn't
        # fill with abandoned bytes.
        target.unlink(missing_ok=True)
        raise

    # ------------------------------------------------------------------
    # 6. Mark the session uploaded. Use bypass_scoping for the UPDATE
    #    because the row is owned by row.user_id and our get_session
    #    dependency does not arm session.info["user_id"] (this endpoint
    #    auth is bearer-vs-hash, not Capa 2 web/MCP middleware).
    # ------------------------------------------------------------------
    with bypass_scoping(db):
        await db.execute(
            UploadSession.__table__.update()
            .where(UploadSession.id == row.id)
            .values(
                status="uploaded",
                uploaded_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    logger.info(
        "upload_received user_id=%s upload_id=%s kind=%s size=%d",
        row.user_id,
        row.id,
        row.kind,
        bytes_written,
    )
    return JSONResponse(
        status_code=200,
        content={"ok": True, "upload_id": str(row.id)},
    )
