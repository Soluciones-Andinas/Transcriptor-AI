"""``POST /api/upload`` + ``POST /api/upload-image`` — RF-MCP-03 / RF-IMG-02.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 2 task 2.4 + G1 + G9 review-fixes):
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
- spec §4 ``INVALID_PARAMETER`` — session.kind mismatch.
- AC-7 image branch — magic-byte sniff via stdlib ``imghdr``,
  bytes land at ``<DATA_DIR>/blobs/<user>/<tx>/<id>.<ext>``.

Auth model (G9 review-fix — operator-facing invariant):
This endpoint authenticates via the EPHEMERAL upload bearer in
``Authorization: Bearer <plaintext>``. The user identity is derived
from ``upload_sessions.user_id`` (a per-row column), NOT from the
ADR-014/015 scoping listener — ``db.info["user_id"]`` is intentionally
NEVER armed in this handler. Any new ORM query in this file MUST
verify ownership manually (via the ``upload_sessions`` row) and run
inside ``bypass_scoping(db)``. The bypass blocks are kept narrow on
purpose: each one wraps exactly the cross-user statement, never the
whole handler body.
"""
from __future__ import annotations

import hmac
import imghdr
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    Query,
    UploadFile,
)
from fastapi.responses import JSONResponse
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.header import parse_bearer
from ..auth.mcp_bearer import hash_bearer
from ..config import settings
from ..db import get_session
from ..db.models import Image, UploadSession
from ..db.scoping import bypass_scoping
from .errors import error_response as _error_resp

logger = logging.getLogger("transcription_api.api.upload")

router = APIRouter(prefix="/api", tags=["uploads"])


@router.post("/upload")
async def upload_audio(
    session_nonce: str = Query(..., alias="session"),
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Chunked-upload endpoint for audio binaries (RF-MCP-03)."""

    # ------------------------------------------------------------------
    # 1. Bearer parse — fast-fail before any DB hit (G7 single source).
    # ------------------------------------------------------------------
    plaintext = parse_bearer(authorization)
    if plaintext is None:
        return _error_resp(
            401, "MCP_BEARER_INVALID", "missing or malformed Authorization header"
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
    # G8.4 — honor settings.upload_session_grace_seconds (RF-MCP-02 §6).
    grace_cutoff = row.expires_at + timedelta(
        seconds=settings.upload_session_grace_seconds
    )
    if datetime.now(timezone.utc) > grace_cutoff:
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
    received_hash = hash_bearer(plaintext)
    if not hmac.compare_digest(received_hash, row.upload_bearer_hash):
        return _error_resp(
            401, "MCP_BEARER_INVALID", "bearer hash mismatch"
        )

    # ------------------------------------------------------------------
    # 5. Stream the body to disk with the +5% margin cap. A cooperative
    #    client sends exactly expected_size_bytes; the margin protects
    #    against compression-frame edge cases without inviting abuse.
    # ------------------------------------------------------------------
    max_bytes = int(row.expected_size_bytes * settings.upload_size_margin)
    target_dir = settings.uploads_dir / str(row.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / settings.upload_raw_filename

    bytes_written = 0
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await file.read(settings.upload_chunk_bytes)
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


# ---------------------------------------------------------------------------
# G1 review-fix — POST /api/upload-image (RF-IMG-02 + RF-MCP-03 image branch).
# ---------------------------------------------------------------------------
# Maps the upload session's ``expected_mime_type`` to the on-disk extension.
# imghdr returns "jpeg" for JPEG bodies; the dict keeps the canonical "jpg"
# extension and the comparison below normalizes the magic-byte name.
_IMAGE_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


@router.post("/upload-image")
async def upload_image(
    session_nonce: str = Query(..., alias="session"),
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Image upload endpoint (RF-IMG-02 + RF-MCP-03 image branch).

    Symmetric to ``upload_audio``: ephemeral bearer is hashed and compared
    against ``upload_sessions.upload_bearer_hash``; the session must have
    ``kind='image'`` and a non-null ``transcription_id`` (the image rides
    into ``images`` linked to that transcription). The body is sniffed with
    stdlib ``imghdr`` to reject magic-byte mismatches, and the bytes land
    under ``DATA_DIR/blobs/<user_id>/<transcription_id>/<image_id>.<ext>``.
    """

    # 1. Bearer parse — fast-fail before any DB hit (G7 single source).
    plaintext = parse_bearer(authorization)
    if plaintext is None:
        return _error_resp(
            401, "MCP_BEARER_INVALID", "missing or malformed Authorization header"
        )

    # 2. Lookup upload session by nonce (cross-user — listener bypass).
    with bypass_scoping(db):
        row = (
            await db.execute(
                select(UploadSession).where(
                    UploadSession.nonce == session_nonce,
                    UploadSession.status == "requested",
                )
            )
        ).scalar_one_or_none()

    # AC-10 parity: unknown nonce OR expired session both return the same
    # 404 with UPLOAD_SESSION_NOT_FOUND (no existence leak).
    if row is None:
        return _error_resp(
            404, "UPLOAD_SESSION_NOT_FOUND", "session not found or already consumed"
        )
    grace_cutoff = row.expires_at + timedelta(
        seconds=settings.upload_session_grace_seconds
    )
    if datetime.now(timezone.utc) > grace_cutoff:
        return _error_resp(404, "UPLOAD_SESSION_NOT_FOUND", "session expired")

    # 3. Kind match — this endpoint is image-only.
    if row.kind != "image":
        return _error_resp(
            400,
            "INVALID_PARAMETER",
            f"endpoint expects kind='image', session has kind={row.kind!r}",
        )

    # 4. Bearer match (constant-time hex compare; G7 hash_bearer single source).
    received_hash = hash_bearer(plaintext)
    if not hmac.compare_digest(received_hash, row.upload_bearer_hash):
        return _error_resp(401, "MCP_BEARER_INVALID", "bearer hash mismatch")

    # 5. Read body with the +5% margin cap.
    max_bytes = int(row.expected_size_bytes * settings.upload_size_margin)
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        return _error_resp(
            413,
            "FILE_TOO_LARGE",
            f"file exceeds expected_size_bytes ({row.expected_size_bytes}) + 5% margin",
            max_bytes=max_bytes,
        )

    # 6. Magic-byte validation against the declared MIME.
    expected_ext = _IMAGE_EXT_BY_MIME.get(row.expected_mime_type or "")
    detected_kind = imghdr.what(None, raw[:32])
    expected_kind = "jpeg" if expected_ext == "jpg" else expected_ext
    if expected_ext is None or detected_kind != expected_kind:
        return _error_resp(
            400,
            "INVALID_FORMAT",
            f"detected={detected_kind!r} expected={row.expected_mime_type!r}",
        )

    # 7. Persist row + bytes. The Image FK to transcriptions.id is NOT NULL,
    # so a session without transcription_id is a malformed upstream call —
    # request_upload_url(kind='image', ...) requires the parameter.
    if row.transcription_id is None:
        return _error_resp(
            400,
            "INVALID_PARAMETER",
            "image upload session missing transcription_id binding",
        )
    image_id = uuid4()
    blob_dir = settings.blobs_dir / str(row.user_id) / str(row.transcription_id)
    blob_dir.mkdir(parents=True, exist_ok=True)
    blob_path = blob_dir / f"{image_id}.{expected_ext}"
    blob_path.write_bytes(raw)

    with bypass_scoping(db):
        await db.execute(
            insert(Image).values(
                id=image_id,
                transcription_id=row.transcription_id,
                user_id=row.user_id,
                filename=file.filename or f"image_{image_id}.{expected_ext}",
                mime_type=row.expected_mime_type,
                size_bytes=len(raw),
                file_path=str(blob_path),
            )
        )
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
        "image_uploaded user_id=%s upload_id=%s image_id=%s size=%d",
        row.user_id,
        row.id,
        image_id,
        len(raw),
    )
    return JSONResponse(
        status_code=200,
        content={"ok": True, "image_id": str(image_id)},
    )
