"""``start_transcription`` tool — RF-MCP-02.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 3):
- AC-1 — Consumes a pre-uploaded ``upload_id``, dispatches to the
  Capa 3 ``orchestrator.orchestrate(...)``, flips the row to
  ``status='consumed'``, removes the per-upload dir, and returns
  ``{transcription_id, status='completed', cache_hit}``.
- AC-9 — ``GPUBusy`` from the orchestrator (lock not acquired within
  ``LOCK_WAIT_SECONDS``) maps to spec ``LOCK_BUSY`` (503).
- AC-10 — Unknown / expired / not-yet-uploaded ``upload_id`` all
  return ``UPLOAD_SESSION_NOT_FOUND`` (404). Cross-user collapses to
  the same code via the listener fail-closed (no existence leak).
- ``UPLOAD_SESSION_ALREADY_CONSUMED`` (409) — second call.
- ``MODELS_NOT_LOADED`` (503) — short-circuit before invoking
  orchestrate (saves the GPU acquire round-trip on a degraded node).
- ``PIPELINE_TIMEOUT`` (504) — orchestrator's typed timeout passes
  through with ``timeout_seconds`` in error data.

The tool reads ``current_user_id`` from the bearer middleware
ContextVar (Batch 1). It opens its own ``mcp_request_session`` so the
listener AND-injects ``WHERE user_id = current_user.id`` on the
upload-row SELECT — a cross-user ``upload_id`` therefore yields no row
and surfaces as ``UPLOAD_SESSION_NOT_FOUND``.

Lazy import of ``transcription_api.main:app`` is intentional: the
``main`` module imports the ``mcp`` package at startup, so a top-level
import here would create a cycle. The lazy form runs at tool-call time
when ``app`` is fully constructed.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select

from ...config import settings
from ...db.models import UploadSession
from ...pipeline.cache import CacheStore
from ...pipeline.orchestrator import GPUBusy, PipelineTimeout, orchestrate
from ..errors import raise_tool_error
from ..middleware import get_current_user_id
from ..server import mcp_server
from ..session import mcp_request_session

logger = logging.getLogger("transcription_api.mcp.tools.transcription")


@mcp_server.tool(name="start_transcription")
async def start_transcription(
    upload_id: str,
    language: str = "es",
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
) -> dict[str, Any]:
    """Run the audio pipeline against a pre-uploaded session.

    Returns ``{transcription_id, status='completed', cache_hit}``.
    Error mapping in module docstring.
    """
    user_id = get_current_user_id()

    try:
        uid = UUID(upload_id)
    except (TypeError, ValueError):
        raise_tool_error(
            "INVALID_PARAMETER",
            f"upload_id is not a valid UUID: {upload_id!r}",
            400,
        )

    # Models gate — short-circuit before opening a DB session if the
    # lifespan never landed both models in 'ready'. Lazy import of
    # main.app avoids the main <-> mcp circular import.
    from ...main import app  # lazy: avoids main <-> mcp cycle

    whisper_status = getattr(app.state, "whisper_status", "loading")
    pyannote_status = getattr(app.state, "pyannote_status", "loading")
    if whisper_status != "ready" or pyannote_status != "ready":
        which = "whisper" if whisper_status != "ready" else "pyannote"
        which_status = whisper_status if which == "whisper" else pyannote_status
        detail = getattr(app.state, f"{which}_detail", None)
        raise_tool_error(
            "MODELS_NOT_LOADED",
            f"{which} model is not ready (status={which_status})",
            503,
            detail=detail,
        )

    whisper_model = app.state.whisper_model
    pyannote_pipeline = app.state.pyannote_pipeline

    async with mcp_request_session(user_id) as db:
        # AC-10 + cross-user — listener AND-injects user_id; foreign rows
        # surface as None and map to UPLOAD_SESSION_NOT_FOUND.
        row = (
            await db.execute(
                select(UploadSession).where(
                    UploadSession.id == uid,
                    UploadSession.kind == "audio",
                )
            )
        ).scalar_one_or_none()

        if row is None:
            raise_tool_error(
                "UPLOAD_SESSION_NOT_FOUND",
                "upload not found",
                404,
            )

        if row.status == "consumed":
            raise_tool_error(
                "UPLOAD_SESSION_ALREADY_CONSUMED",
                "this upload has already been consumed by start_transcription",
                409,
            )

        # Anything that is not 'uploaded' (still 'requested', or expired)
        # collapses to NOT_FOUND per AC-10 — same body as cross-user /
        # unknown id (no existence leak across causes).
        if row.status != "uploaded" or row.expires_at < datetime.now(timezone.utc):
            raise_tool_error(
                "UPLOAD_SESSION_NOT_FOUND",
                "upload not yet received or expired",
                404,
            )

        upload_dir_for_session = settings.uploads_dir / str(uid)
        original = upload_dir_for_session / "original.bin"

        cache_store = CacheStore(base_dir=settings.cache_dir)

        # Capa 3 invariant: orchestrate handles the lock+timeout+pipeline
        # body. We never touch _orchestrator_lock directly. GPUBusy /
        # PipelineTimeout are typed exceptions with structured fields we
        # surface to the MCP client.
        try:
            result = await orchestrate(
                user_id=user_id,
                db=db,
                file_path=original,
                # D-048: upload_sessions has no original_filename column;
                # synthesize from upload id. Wiki should add the column
                # in Capa 5+ if real filenames matter for audit.
                original_filename=f"upload_{uid}.bin",
                original_size_bytes=row.expected_size_bytes,
                whisper_model=whisper_model,
                pyannote_pipeline=pyannote_pipeline,
                cache_store=cache_store,
                upload_dir=upload_dir_for_session,
                language=language,
                num_speakers=num_speakers,
                min_speakers=min_speakers,
                max_speakers=max_speakers,
            )
        except GPUBusy as exc:
            raise_tool_error(
                "LOCK_BUSY",
                f"GPU busy; retry after {exc.retry_after}s",
                503,
                retry_after=exc.retry_after,
            )
        except PipelineTimeout as exc:
            raise_tool_error(
                "PIPELINE_TIMEOUT",
                f"pipeline exceeded {exc.timeout_seconds}s",
                504,
                timeout_seconds=exc.timeout_seconds,
            )

        # Mark the upload consumed AFTER orchestrate succeeds — keeps
        # the row recoverable on failure (a future retry can still
        # find it 'uploaded' and re-run).
        await db.execute(
            UploadSession.__table__.update()
            .where(UploadSession.id == uid)
            .values(
                status="consumed",
                consumed_at=datetime.now(timezone.utc),
            )
        )
        # mcp_request_session ctx mgr commits on exit.

    # Best-effort cleanup of the per-upload dir. orchestrate already
    # removed the normalized WAV (Capa 3 contract); we remove the raw
    # original.bin + its parent dir so DATA_DIR/uploads/ stays small.
    try:
        if upload_dir_for_session.exists():
            shutil.rmtree(upload_dir_for_session)
    except OSError:
        logger.warning(
            "upload_dir_cleanup_failed path=%s error_id=UPLOAD_DIR_LEAK",
            upload_dir_for_session,
            exc_info=True,
        )

    return {
        "transcription_id": str(result["transcription_id"]),
        "status": "completed",
        "cache_hit": result.get("metadata", {}).get("cache_hit", False),
    }
