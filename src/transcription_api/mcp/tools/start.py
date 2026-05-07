"""``start_transcription`` tool — RF-MCP-02.

Spec: SPEC-capa4-mcp-v1
Covers (G6 — review-fixes Tier 2 split):
- AC-1, AC-9 (LOCK_BUSY), AC-10, AC-15, AC-16 — runs the audio
  pipeline against a pre-uploaded session, flips ``upload_sessions``
  to ``status='consumed'``, removes the per-upload directory in a
  ``finally`` block (G2 review-fix), and surfaces typed pipeline
  exceptions (``GPUBusy`` -> LOCK_BUSY, ``PipelineTimeout`` ->
  PIPELINE_TIMEOUT).
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

logger = logging.getLogger("transcription_api.mcp.tools.start")


async def _load_upload_row(db, upload_id: str, user_id: UUID):
    """Resolve an uploaded session for the caller, raising typed errors.

    Walks the same state-machine the prior inline body covered:

    - invalid UUID                        -> INVALID_PARAMETER (400)
    - row missing OR status='requested' -> UPLOAD_SESSION_NOT_FOUND (404)
    - status='consumed'                   -> UPLOAD_SESSION_ALREADY_CONSUMED (409)
    - past expires_at + grace             -> UPLOAD_SESSION_NOT_FOUND (404)

    Cross-user isolation continues to flow from the listener's
    AND-injection on ``mcp_request_session`` — the lookup itself is a
    naive ``WHERE id = ...`` that the listener augments to scope.
    """
    try:
        uid = UUID(upload_id)
    except (TypeError, ValueError):
        raise_tool_error(
            "INVALID_PARAMETER",
            f"upload_id is not a valid UUID: {upload_id!r}",
            400,
        )
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
    if row.status != "uploaded":
        raise_tool_error(
            "UPLOAD_SESSION_NOT_FOUND",
            "upload not yet received or expired",
            404,
        )
    grace_cutoff = row.expires_at + timedelta(
        seconds=settings.upload_session_grace_seconds
    )
    if datetime.now(timezone.utc) > grace_cutoff:
        raise_tool_error(
            "UPLOAD_SESSION_NOT_FOUND",
            "upload not yet received or expired",
            404,
        )
    return row


async def _consume_upload(db, row) -> None:
    """Flip the upload session to ``status='consumed'`` (post-orchestrate)."""
    await db.execute(
        UploadSession.__table__.update()
        .where(UploadSession.id == row.id)
        .values(
            status="consumed",
            consumed_at=datetime.now(timezone.utc),
        )
    )


def _cleanup_upload_dir(upload_dir: Path) -> None:
    """Best-effort removal of the per-upload directory.

    Logs at WARNING with ``error_id=UPLOAD_DIR_LEAK`` on rmtree failure;
    never re-raises so the caller's outer try/finally invariant holds.
    """
    if not upload_dir.exists():
        return
    try:
        shutil.rmtree(upload_dir)
    except OSError:
        logger.warning(
            "upload_dir_cleanup_failed path=%s error_id=UPLOAD_DIR_LEAK",
            upload_dir,
            exc_info=True,
        )


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

    G2 review-fix: cleanup of ``DATA_DIR/uploads/<id>/`` runs in a
    ``finally`` block tied to the orchestrate window. Prior implementation
    cleaned post-context-manager, so any exception (typed or generic)
    leaked the directory.
    """
    user_id = get_current_user_id()

    # Models gate — read the runtime snapshot armed by the bearer
    # middleware (G12 review-fix). The middleware lazy-imports main.app
    # exactly once per request and snapshots whisper / pyannote /
    # readiness into ContextVars; this tool no longer touches main.app.
    from ..runtime import get_runtime_models, get_runtime_status

    status = get_runtime_status()
    if status is None:
        # Tool invoked outside a request flow that ran the middleware
        # (e.g., a misconfigured test path). Surface as MODELS_NOT_LOADED
        # rather than an AttributeError on a None app.state.
        raise_tool_error(
            "MODELS_NOT_LOADED",
            "runtime context not armed (middleware did not run)",
            503,
        )
    if (
        status["whisper_status"] != "ready"
        or status["pyannote_status"] != "ready"
    ):
        # Whisper takes precedence on a double-fault to mirror the
        # ``check_models_ready`` helper used by the REST endpoint
        # (G2 dedupe stays in lock step).
        if status["whisper_status"] != "ready":
            failing = "whisper"
            detail = status["whisper_detail"]
        else:
            failing = "pyannote"
            detail = status["pyannote_detail"]
        raise_tool_error(
            "MODELS_NOT_LOADED",
            f"{failing} model is not ready",
            503,
            detail=detail,
        )

    whisper_model, pyannote_pipeline = get_runtime_models()
    cache_store = CacheStore(base_dir=settings.cache_dir)

    async with mcp_request_session(user_id) as db:
        row = await _load_upload_row(db, upload_id, user_id)
        upload_dir = settings.uploads_dir / str(row.id)
        original = upload_dir / settings.upload_raw_filename

        try:
            try:
                result = await orchestrate(
                    user_id=user_id,
                    db=db,
                    file_path=original,
                    # D-048: upload_sessions has no original_filename column;
                    # synthesize from upload id. Wiki should add the column
                    # in Capa 5+ if real filenames matter for audit.
                    original_filename=f"upload_{row.id}.bin",
                    original_size_bytes=row.expected_size_bytes,
                    whisper_model=whisper_model,
                    pyannote_pipeline=pyannote_pipeline,
                    cache_store=cache_store,
                    upload_dir=upload_dir,
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
            await _consume_upload(db, row)
            return {
                "transcription_id": str(result["transcription_id"]),
                "status": "completed",
                "cache_hit": result.get("metadata", {}).get("cache_hit", False),
            }
        finally:
            _cleanup_upload_dir(upload_dir)
