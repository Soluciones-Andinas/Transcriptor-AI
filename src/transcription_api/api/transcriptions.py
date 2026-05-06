"""POST + GET /api/transcriptions — wraps the orchestrator with HTTP semantics.

Spec: SPEC-capa3-pipeline-v1
Covers (Batch 6):
- AC-1 — POST returns 200 + JSON with transcription_id, audio_hash,
  language, segments, duration_seconds, num_speakers, text_content.
- AC-3 — Bearer auth via Capa 2's ``get_current_user_mcp`` dependency.
  No bearer / malformed -> 401 AUTH_NOT_AUTHENTICATED (handled by the
  dependency itself, not by this module).
- error catalog (spec §4) — orchestrator typed errors are mapped to
  HTTP per the catalog: GPUBusy -> 503+Retry-After, PipelineTimeout
  -> 504, GPUError -> 500, PipelineNormalizeError -> 500,
  PipelineDiarizeError -> 500, AudioFormatInvalid -> 400, generic
  Exception -> 500 INTERNAL_ERROR with an ``error_id`` UUID for log
  correlation.

Transaction semantics (D-037 follow-up): the orchestrator does
``flush()``, not ``commit()``. This module commits on the success path
and rollbacks on every typed-error branch so a partial INSERT never
escapes a failed request.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user_mcp
from ..config import settings
from ..db import get_session
from ..db.models import User
from ..pipeline.cache import CacheStore
from ..pipeline.diarize import PipelineDiarizeError
from ..pipeline.normalize import AudioFormatInvalid, PipelineNormalizeError
from ..pipeline.orchestrator import GPUBusy, PipelineTimeout, orchestrate
from ..pipeline.stt import GPUError

logger = logging.getLogger("transcription_api.api.transcriptions")

router = APIRouter(prefix="/api", tags=["transcriptions"])


def _serialize_for_json(value: Any) -> Any:
    """Convert UUIDs to str so JSONResponse can serialize the dict.

    The orchestrator returns a dict whose ``transcription_id`` is a
    ``uuid.UUID``. Recursing into segments / metadata keeps the wider
    shape intact in case future fields surface UUIDs.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _serialize_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize_for_json(v) for v in value]
    return value


@router.post("/transcriptions")
async def post_transcription(
    request: Request,
    file: UploadFile = File(...),
    language: str = Form("es"),
    num_speakers: int | None = Form(None),
    min_speakers: int | None = Form(None),
    max_speakers: int | None = Form(None),
    user: User = Depends(get_current_user_mcp),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """POST /api/transcriptions — multipart upload + bearer + orchestrate."""

    # Save raw upload to settings.uploads_dir.
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    upload_filename = file.filename or "upload"
    suffix = Path(upload_filename).suffix.lower()
    raw_id = str(uuid.uuid4())
    raw_path = settings.uploads_dir / f"{raw_id}{suffix}"

    bytes_written = 0
    try:
        with raw_path.open("wb") as fh:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                fh.write(chunk)
    except Exception:
        raw_path.unlink(missing_ok=True)
        raise

    cache_store = CacheStore(base_dir=settings.cache_dir)

    try:
        result = await orchestrate(
            user_id=user.id,
            db=db,
            file_path=raw_path,
            original_filename=upload_filename,
            original_size_bytes=bytes_written,
            whisper_model=request.app.state.whisper_model,
            pyannote_pipeline=request.app.state.pyannote_pipeline,
            cache_store=cache_store,
            upload_dir=settings.uploads_dir,
            language=language,
            num_speakers=num_speakers,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
        )
        # D-037 follow-up: orchestrator flushes; we commit.
        await db.commit()
        return JSONResponse(status_code=200, content=_serialize_for_json(result))

    except GPUBusy as exc:
        await db.rollback()
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "error_code": "GPU_BUSY",
                    "reason": "GPU busy; retry shortly",
                    "retry_after": exc.retry_after,
                }
            },
            headers={"Retry-After": str(exc.retry_after)},
        )

    except PipelineTimeout as exc:
        await db.rollback()
        return JSONResponse(
            status_code=504,
            content={
                "detail": {
                    "error_code": "PIPELINE_TIMEOUT",
                    "reason": f"pipeline exceeded {exc.timeout_seconds}s",
                    "timeout_seconds": exc.timeout_seconds,
                }
            },
        )

    except GPUError as exc:
        await db.rollback()
        return JSONResponse(
            status_code=500,
            content={
                "detail": {
                    "error_code": "GPU_ERROR",
                    "reason": str(exc),
                }
            },
        )

    except PipelineDiarizeError as exc:
        await db.rollback()
        return JSONResponse(
            status_code=500,
            content={
                "detail": {
                    "error_code": "PIPELINE_DIARIZE_ERROR",
                    "reason": str(exc),
                }
            },
        )

    except PipelineNormalizeError as exc:
        await db.rollback()
        return JSONResponse(
            status_code=500,
            content={
                "detail": {
                    "error_code": "PIPELINE_NORMALIZE_ERROR",
                    "reason": str(exc),
                }
            },
        )

    except AudioFormatInvalid as exc:
        await db.rollback()
        return JSONResponse(
            status_code=400,
            content={
                "detail": {
                    "error_code": "AUDIO_FORMAT_INVALID",
                    "reason": str(exc),
                }
            },
        )

    except Exception:
        # Catch-all -> 500 INTERNAL_ERROR with a correlation UUID. The
        # traceback lives in the logs (NOT in the response body — that
        # would leak filesystem paths and stack frames).
        error_id = str(uuid.uuid4())
        logger.exception("internal_error error_id=%s", error_id)
        await db.rollback()
        return JSONResponse(
            status_code=500,
            content={
                "detail": {
                    "error_code": "INTERNAL_ERROR",
                    "reason": "see error_id in logs",
                    "error_id": error_id,
                }
            },
        )

    finally:
        # Best-effort cleanup of the raw upload (the orchestrator already
        # cleaned the normalized WAV in its own finally).
        try:
            raw_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "raw_upload_cleanup_failed path=%s", raw_path, exc_info=True
            )
