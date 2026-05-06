"""Audio pipeline orchestrator with global GPU lock.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-6 — Module-level ``_orchestrator_lock = asyncio.Lock()`` (one per
  process, ADR-005: one GPU job at a time). Concurrent ``orchestrate``
  calls serialize: the first holds the lock, the second waits up to
  ``lock_wait_seconds`` and raises ``GPUBusy(retry_after=...)``.
- AC-7 — A ``try/finally`` around the inner pipeline body releases the
  lock no matter what fails inside (typed pipeline errors, generic
  RuntimeError, asyncio cancellation). Tests assert the lock is
  unlocked after every failure mode.
- AC-11 — The inner pipeline body is wrapped in
  ``asyncio.wait_for(..., timeout=pipeline_timeout_seconds)`` so a
  hung pipeline raises ``PipelineTimeout(timeout_seconds=...)`` after
  the configured deadline. Distinct exception type from ``GPUBusy``
  so Batch 6 can map them to different HTTP responses (503 with
  Retry-After vs 504).
- AC-1 (orchestrator side, T5.4 — pending) — End-to-end
  normalize → cache lookup → STT → diarize → merge → persist with
  per-user cache and DB INSERT inside ``bypass_scoping(db)``.

The unit of work is ``_run_pipeline`` — the documented patchable seam
that lets tests exercise the lock+timeout wrapper without touching
ffmpeg/torch/pyannote/Postgres (mirrors the ``_whisperx_load_model`` /
``_pyannote_run_pipeline`` indirections from prior batches, D-030).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

from ..config import settings


# Module-level singleton: one GPU job at a time per FastAPI worker
# process (ADR-005). Re-binding this in tests is intentional in some
# concurrency tests; production code never reassigns it.
_orchestrator_lock: asyncio.Lock = asyncio.Lock()


class GPUBusy(Exception):
    """Lock could NOT be acquired within ``lock_wait_seconds``.

    The HTTP layer (Batch 6) maps this to 503 ``GPU_BUSY`` with
    ``Retry-After: retry_after``. Distinct from ``PipelineTimeout``
    because the GPU never started this job — a quick retry has a real
    chance of succeeding.
    """

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"GPU busy; retry after {retry_after} seconds")


class PipelineTimeout(Exception):
    """Lock WAS acquired but the inner pipeline ran longer than
    ``pipeline_timeout_seconds``.

    The HTTP layer maps this to 504 ``PIPELINE_TIMEOUT``. Distinct from
    ``GPUBusy``: a retry of the same audio with the same parameters is
    likely to time out again, so the client typically escalates to the
    user (try a shorter clip, retry off-peak, etc.).
    """

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Pipeline exceeded {timeout_seconds}s timeout"
        )


async def _run_pipeline(
    *,
    user_id: UUID,
    db: Any,
    file_path: Path,
    original_filename: str,
    original_size_bytes: int,
    whisper_model: Any,
    pyannote_pipeline: Any,
    cache_store: Any,
    upload_dir: Path,
    language: str,
    num_speakers: int | None,
    min_speakers: int | None,
    max_speakers: int | None,
) -> dict[str, Any]:
    """Inner pipeline: normalize → cache → stt → diarize → merge → persist.

    Stub for T5.1–T5.3 — those tasks exercise the surrounding lock and
    timeout wrappers and patch this function out. T5.4 fills the body.
    Raises ``NotImplementedError`` so a missing patch is loud, not silent.
    """
    raise NotImplementedError(
        "_run_pipeline body lands in T5.4; tests must patch this seam"
    )


async def orchestrate(
    *,
    user_id: UUID,
    db: Any,
    file_path: Path,
    original_filename: str,
    original_size_bytes: int,
    whisper_model: Any,
    pyannote_pipeline: Any,
    cache_store: Any,
    upload_dir: Path,
    language: str = "es",
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    lock_timeout: float | None = None,
    retry_after: int | None = None,
    pipeline_timeout: float | None = None,
) -> dict[str, Any]:
    """Acquire GPU lock, run the inner pipeline with a timeout, release.

    The ``lock_timeout`` / ``retry_after`` / ``pipeline_timeout`` kwargs
    let tests (and Batch 6 if needed) override the settings defaults
    without monkey-patching ``settings``. Production callers omit them.
    """
    lock_to = (
        lock_timeout if lock_timeout is not None else settings.lock_wait_seconds
    )
    retry = (
        retry_after
        if retry_after is not None
        else settings.lock_retry_after_seconds
    )
    pipe_to = (
        pipeline_timeout
        if pipeline_timeout is not None
        else settings.pipeline_timeout_seconds
    )

    try:
        await asyncio.wait_for(_orchestrator_lock.acquire(), timeout=lock_to)
    except asyncio.TimeoutError as exc:
        # Lock NOT acquired — nothing to release in finally below.
        raise GPUBusy(retry_after=retry) from exc

    try:
        return await asyncio.wait_for(
            _run_pipeline(
                user_id=user_id,
                db=db,
                file_path=file_path,
                original_filename=original_filename,
                original_size_bytes=original_size_bytes,
                whisper_model=whisper_model,
                pyannote_pipeline=pyannote_pipeline,
                cache_store=cache_store,
                upload_dir=upload_dir,
                language=language,
                num_speakers=num_speakers,
                min_speakers=min_speakers,
                max_speakers=max_speakers,
            ),
            timeout=pipe_to,
        )
    except asyncio.TimeoutError as exc:
        raise PipelineTimeout(timeout_seconds=pipe_to) from exc
    finally:
        # AC-7: release the lock no matter what propagated through the try.
        # ``locked()`` guard is defensive — under normal flow we always hold
        # it here, but a future refactor that re-arranges the acquire could
        # otherwise raise ``RuntimeError: Lock is not acquired``.
        if _orchestrator_lock.locked():
            _orchestrator_lock.release()
