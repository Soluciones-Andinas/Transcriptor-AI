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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user_mcp
from ..config import settings
from ..db import get_session
from ..db.models import Transcription, User
from ..pipeline.cache import CacheStore
from ..pipeline.diarize import PipelineDiarizeError
from ..pipeline.normalize import (
    AudioFormatInvalid,
    AudioTooLong,
    PipelineNormalizeError,
)
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


def _audio_too_large_response(max_mb: int) -> JSONResponse:
    """Build the 413 response for AC-5 violations."""
    return JSONResponse(
        status_code=413,
        content={
            "detail": {
                "error_code": "AUDIO_TOO_LARGE",
                "reason": f"upload exceeds {max_mb} MB",
                "max_mb": max_mb,
            }
        },
    )


def _stripped_500(
    exc: Exception, error_code: str, *, extra: dict[str, Any] | None = None
) -> JSONResponse:
    """500 response with a stable reason + correlation ``error_id`` (H-3).

    Capa 3 review H-3: the prior implementations did
    ``"reason": str(exc)`` for GPUError / PipelineDiarizeError /
    PipelineNormalizeError. For ffmpeg / CUDA failures the upstream
    message routinely contains filesystem paths, driver versions, or
    binary stderr — leaked verbatim to the user. The spec catalog
    (§4) says ``"detail": <stripped>``; this helper enforces that.

    The full ``str(exc)`` is logged with the error_id so support can
    correlate via the operator's stdout. Optional ``extra`` carries
    typed discriminators (e.g., ``GPUError.detail``) that ARE safe to
    expose because they come from a closed enum.
    """
    error_id = str(uuid.uuid4())
    logger.error(
        "%s error_id=%s detail=%s",
        error_code,
        error_id,
        exc,
        exc_info=True,
    )
    body: dict[str, Any] = {
        "error_code": error_code,
        "reason": "see error_id in logs",
        "error_id": error_id,
    }
    if extra:
        body.update(extra)
    return JSONResponse(status_code=500, content={"detail": body})


def _models_loaded_or_503(request: Request) -> JSONResponse | None:
    """AC-15 short-circuit: 503 MODELS_NOT_LOADED if either model is not ready.

    Thin REST adapter over ``runtime.readiness.check_models_ready`` —
    the precedence and detail-field semantics live there so the MCP
    tool ``start_transcription`` and this REST endpoint stay in lock
    step (G2 dedupe).
    """
    from ..runtime.readiness import check_models_ready

    res = check_models_ready(request.app.state)
    if res.ready:
        return None

    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "error_code": "MODELS_NOT_LOADED",
                "reason": (
                    f"{res.failing_model} model is not ready; "
                    "service is starting or degraded"
                ),
                "detail": res.detail,
            }
        },
        headers={"Retry-After": "30"},
    )


@router.post("/transcriptions", deprecated=True)
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
    """POST /api/transcriptions — multipart upload + bearer + orchestrate.

    Capa 4 AC-16 / D-026: this endpoint is marked ``deprecated=True`` in
    OpenAPI and emits a WARN ``legacy_endpoint_invoked`` on every
    invocation. It still works (rig smoke-test depends on it); removal
    is scheduled for Capa 5. The MCP flow ``request_upload_url`` ->
    ``POST /api/upload`` -> ``start_transcription`` is the replacement.
    """

    # AC-16 — emit one WARN per invocation BEFORE any other branch so
    # 503 / 401 / 413 paths still increment the legacy-usage counter.
    logger.warning(
        "legacy_endpoint_invoked "
        "deprecated_endpoint=POST_/api/transcriptions "
        "removal_target=Capa5"
    )

    # AC-15 — short-circuit BEFORE accepting the upload if either model
    # failed to load. Saves the network round-trip on a degraded service.
    not_ready = _models_loaded_or_503(request)
    if not_ready is not None:
        return not_ready

    # AC-5 — Content-Length pre-check. Cheap reject for hostile / large
    # uploads BEFORE we spend any bytes on disk or invoke orchestrate.
    # A client that lies about Content-Length still hits the streaming
    # cap below; the pre-check just shaves the common case.
    max_bytes = settings.max_upload_mb * 1024 * 1024
    content_length_header = request.headers.get("content-length")
    if content_length_header is not None:
        try:
            declared = int(content_length_header)
        except ValueError:
            declared = None  # malformed header; fall through to streaming cap
        if declared is not None and declared > max_bytes:
            return _audio_too_large_response(settings.max_upload_mb)

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
                # AC-5 streaming cap: clients that lied (or omitted)
                # Content-Length still get caught here. Cleanup partial
                # write and short-circuit.
                if bytes_written > max_bytes:
                    fh.close()
                    raw_path.unlink(missing_ok=True)
                    return _audio_too_large_response(settings.max_upload_mb)
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
        # H-3: keep the typed `detail` field (closed enum: oom / runtime)
        # but DO NOT leak the upstream str(exc) — CUDA error strings
        # routinely contain filesystem paths and driver versions.
        await db.rollback()
        return _stripped_500(exc, "GPU_ERROR", extra={"detail": exc.detail})

    except PipelineDiarizeError as exc:
        # H-3: pyannote / Hugging Face errors may leak HF cache paths.
        await db.rollback()
        return _stripped_500(exc, "PIPELINE_DIARIZE_ERROR")

    except PipelineNormalizeError as exc:
        # H-3: ffmpeg stderr is the most leaky surface (always contains
        # the input path, often the user's home directory). Stripped
        # to the error_id correlation pattern; full message in logs.
        await db.rollback()
        return _stripped_500(exc, "PIPELINE_NORMALIZE_ERROR")

    except AudioFormatInvalid as exc:
        # NOT stripped — the message is already a Spanish-readable
        # validation reason ("extensión .X no soportada; ..."), no
        # filesystem leak. The end user benefits from seeing it.
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

    except AudioTooLong as exc:
        # SD-1: duration cap (MAX_AUDIO_DURATION_SECONDS, spec §7) —
        # surfaced as the same 413 AUDIO_TOO_LARGE category as the byte
        # cap (AC-5). The body distinguishes the cause via the optional
        # `duration_seconds` / `max_seconds` fields so the client can
        # render an actionable error message.
        await db.rollback()
        return JSONResponse(
            status_code=413,
            content={
                "detail": {
                    "error_code": "AUDIO_TOO_LARGE",
                    "reason": (
                        f"audio duration {exc.duration_seconds:.1f}s exceeds "
                        f"{exc.max_seconds}s cap"
                    ),
                    "duration_seconds": exc.duration_seconds,
                    "max_seconds": exc.max_seconds,
                }
            },
        )

    except Exception as exc:
        # Catch-all -> 500 INTERNAL_ERROR. Same H-3 stripping pattern.
        await db.rollback()
        return _stripped_500(exc, "INTERNAL_ERROR")

    finally:
        # Best-effort cleanup of the raw upload (the orchestrator already
        # cleaned the normalized WAV in its own finally).
        try:
            raw_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "raw_upload_cleanup_failed path=%s", raw_path, exc_info=True
            )


def _row_to_response(row: Transcription) -> dict[str, Any]:
    """Transcription row -> POST-equivalent response dict.

    Matches the shape returned by ``orchestrate``. The JSONB column
    ``segments`` was persisted as ``{"segments": [...]}`` (matches the
    factory pattern); we unwrap the list back so the API response is
    a flat array, identical to what POST returned.
    """
    segments_blob = row.segments or {}
    segments = (
        segments_blob.get("segments", [])
        if isinstance(segments_blob, dict)
        else []
    )
    return {
        "transcription_id": row.id,
        "audio_hash": row.audio_hash,
        "language": row.language,
        "duration_seconds": float(row.duration_seconds),
        "num_speakers": row.num_speakers,
        "text_content": row.text_content,
        "segments": segments,
        "metadata": row.extra_metadata,
    }


@router.get("/transcriptions/{transcription_id}")
async def get_transcription(
    transcription_id: UUID,
    user: User = Depends(get_current_user_mcp),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """GET /api/transcriptions/{id} — owner-scoped via the listener.

    The ADR-014/015 listener AND-injects ``WHERE user_id = X`` because
    ``get_current_user_mcp`` armed ``db.info["user_id"] = current_user.id``.
    A row owned by another user is filtered out and ``scalar_one_or_none``
    returns ``None``; we 404 with the SAME response shape as a fully-
    nonexistent id (AC-8 — no existence leak).
    """
    row = (
        await db.execute(
            select(Transcription).where(Transcription.id == transcription_id)
        )
    ).scalar_one_or_none()

    if row is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": {
                    "error_code": "TRANSCRIPTION_NOT_FOUND",
                    "reason": "transcription not found",
                }
            },
        )

    return JSONResponse(
        status_code=200,
        content=_serialize_for_json(_row_to_response(row)),
    )
