"""Audio pipeline orchestrator with global GPU lock.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-6 — Module-level ``_orchestrator_lock`` (one per process, ADR-005:
  one GPU job at a time). Concurrent ``orchestrate`` calls serialize:
  the first holds the lock, the second waits up to ``lock_wait_seconds``
  and raises ``GPUBusy(retry_after=...)``.
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
- AC-1 (orchestrator side) — End-to-end normalize → cache lookup → STT
  → diarize → merge → persist with per-user cache and DB INSERT inside
  ``bypass_scoping(db)``.

Capa 3 review fixes (CR-1 + CR-2):

- ``_OrchestrationLock`` (custom class below) wraps ``asyncio.Lock``
  with owner-task tracking. Closes the BPO-31647 race window where
  ``asyncio.wait_for(lock.acquire(), timeout=...)`` can return
  ``TimeoutError`` while the underlying lock IS acquired. The
  owner-task field lets the caller's TimeoutError handler check
  ``held_by_current_task()`` and release safely.
- Explicit ``acquired`` flag in ``orchestrate()`` replaces the prior
  ``if _orchestrator_lock.locked(): release()`` defensive guard which
  was unsound (``locked()`` is global state — could be True because
  another task holds it; releasing would unlock theirs incorrectly).

The unit of work is ``_run_pipeline`` — the documented patchable seam
that lets tests exercise the lock+timeout wrapper without touching
ffmpeg/torch/pyannote/Postgres (mirrors the ``_whisperx_load_model`` /
``_pyannote_run_pipeline`` indirections from prior batches, D-030).
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from ..config import settings
from ..db.scoping import bypass_scoping
from . import diarize as _diarize_mod
from . import normalize as _normalize_mod
from . import stt as _stt_mod
from .merge import assign_speakers_to_words

logger = logging.getLogger("transcription_api.pipeline")


class _OrchestrationLock:
    """asyncio.Lock variant that tracks the owning task for BPO-31647 safety.

    Drop-in replacement for ``asyncio.Lock`` plus ``held_by_current_task()``.
    Used to safely fix the wait_for-acquire race documented in BPO-31647:
    when ``asyncio.wait_for(lock.acquire(), timeout=...)`` raises
    ``TimeoutError`` after acquire succeeded, the caller can call
    ``held_by_current_task()`` to detect the race and release safely.

    The internal ``acquire()`` also handles the tighter race where
    cancellation arrives between ``self._lock.acquire()`` returning and
    ``self._owner_task = ...`` being set: the except branch checks if
    the underlying lock is held with no owner-task and releases manually.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner_task: asyncio.Task[Any] | None = None

    async def acquire(self) -> None:
        try:
            await self._lock.acquire()
        except asyncio.CancelledError:
            # Cancellation can land between acquire's atomic step and
            # this function's return. If the underlying lock IS held
            # but no owner-task is set, we just acquired it; release.
            if self._lock.locked() and self._owner_task is None:
                try:
                    self._lock.release()
                except RuntimeError:
                    # Lock was released externally — nothing to do.
                    pass
            raise
        else:
            self._owner_task = asyncio.current_task()

    def release(self) -> None:
        self._owner_task = None
        self._lock.release()

    def held_by_current_task(self) -> bool:
        """True iff the lock is held AND its owner is the current task.

        Used by callers that need to distinguish "I hold the lock"
        from "someone holds it" — critical for BPO-31647 cleanup.
        """
        return (
            self._owner_task is not None
            and self._owner_task is asyncio.current_task()
        )

    def locked(self) -> bool:
        """Compat shim: True if any task holds the underlying lock."""
        return self._lock.locked()


# Module-level singleton: one GPU job at a time per FastAPI worker
# process (ADR-005). Re-binding this in tests is intentional in some
# concurrency tests; production code never reassigns it.
_orchestrator_lock: _OrchestrationLock = _OrchestrationLock()


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


def _build_text_content(merged_segments: list[dict[str, Any]]) -> str:
    """Render merged segments as ``SPEAKER_xx: text`` lines for full-text search.

    The Postgres GIN tsvector index lives on ``transcriptions.text_content``,
    so the indexed string must include the spoken words AND the speaker
    label so future search ("what did SPEAKER_00 say about X") can hit.
    """
    lines = []
    for segment in merged_segments:
        speaker = segment.get("speaker", "UNKNOWN")
        text = segment.get("text", "")
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


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

    The function is patchable as ``transcription_api.pipeline.orchestrator
    ._run_pipeline``; tests for AC-6 / AC-7 / AC-11 substitute it with a
    sleeper or a thrower to exercise the lock/timeout wrapper around it.

    Production flow:
    1. ffmpeg normalize → ``(wav_path, audio_hash, duration)``.
    2. Per-user cache lookup. On hit (ALT-1) skip steps 3-5 and mark
       ``metadata.cache_hit: true``; still persist a fresh row so the
       user's history grows (D-027).
    3. WhisperX transcribe. Empty segments (ALT-2 silent audio) skip
       diarize+merge and persist with ``text_content=""``,
       ``num_speakers=0``, ``metadata.silent_audio: true``.
    4. pyannote diarize.
    5. Merge word-level transcript with diarization speakers.
    6. Cache write (canonical ``cache_hit: false`` so a future read hits
       and overrides to true).
    7. INSERT ``Transcription`` row inside ``bypass_scoping(db)`` —
       declares the cross-bootstrap intent: the row's ``user_id`` IS the
       active user, and an over-eager listener filter on the INSERT
       statement would AND the same predicate twice (harmless but
       inelegant). The bypass keeps the SQL emitted exactly what we
       built.
    8. Cleanup the normalized WAV in ``finally`` (Privacy > Cost — no
       intermediate audio lingers on disk after the pipeline returns).
    """
    # ------------------------------------------------------------------
    # 1. Normalize (CPU-bound, run in thread pool to keep the loop free).
    # ------------------------------------------------------------------
    normalized_wav, audio_hash, duration_float = await asyncio.to_thread(
        _normalize_mod.normalize_audio, file_path, upload_dir
    )

    try:
        # --------------------------------------------------------------
        # 2. Cache lookup (per-user — D-027). Validation happens inside
        # CacheStore; an invalid audio_hash from a buggy normalize raises
        # ValueError which propagates and the orchestrator's outer
        # try/finally releases the lock.
        # --------------------------------------------------------------
        cached = cache_store.get(user_id, audio_hash)
        if cached is not None:
            payload = {
                **cached,
                "audio_hash": audio_hash,
                "metadata": {**cached.get("metadata", {}), "cache_hit": True},
            }
        else:
            # ----------------------------------------------------------
            # 3. STT (GPU-bound — `transcribe` is sync; thread-pool
            # offload keeps the event loop responsive).
            # ----------------------------------------------------------
            stt_result = await asyncio.to_thread(
                _stt_mod.transcribe,
                whisper_model,
                normalized_wav,
                language=language,
            )
            stt_segments = stt_result.get("segments", [])
            detected_language = stt_result.get("language", language)

            if not stt_segments:
                # ALT-2 — silent audio. Skip diarize+merge.
                payload = {
                    "audio_hash": audio_hash,
                    "language": detected_language,
                    "duration_seconds": duration_float,
                    "num_speakers": 0,
                    "text_content": "",
                    "segments": [],
                    "metadata": {
                        "model": settings.whisper_model,
                        "diarizer": settings.pyannote_model,
                        "compute_type": settings.compute_type,
                        "cache_hit": False,
                        "silent_audio": True,
                    },
                }
            else:
                # ------------------------------------------------------
                # 4. Diarize (GPU-bound — same thread-pool rationale).
                # ------------------------------------------------------
                diarization = await asyncio.to_thread(
                    _diarize_mod.diarize,
                    pyannote_pipeline,
                    normalized_wav,
                    num_speakers=num_speakers,
                    min_speakers=min_speakers,
                    max_speakers=max_speakers,
                )
                # 5. Merge — pure Python, fast enough to stay in the loop.
                merged_segments = assign_speakers_to_words(
                    stt_segments, diarization
                )
                unique_speakers = {
                    seg.get("speaker")
                    for seg in merged_segments
                    if seg.get("speaker")
                }
                payload = {
                    "audio_hash": audio_hash,
                    "language": detected_language,
                    "duration_seconds": duration_float,
                    "num_speakers": len(unique_speakers),
                    "text_content": _build_text_content(merged_segments),
                    "segments": merged_segments,
                    "metadata": {
                        "model": settings.whisper_model,
                        "diarizer": settings.pyannote_model,
                        "compute_type": settings.compute_type,
                        "cache_hit": False,
                    },
                }

            # 6. Cache write — store canonical (cache_hit=False) version.
            # A future read overrides to True at lookup time (above).
            cache_store.put(user_id, audio_hash, payload)

        # --------------------------------------------------------------
        # 7. Persist row. Bootstrap insert: the row's user_id IS the
        # active user, so the listener's WHERE injection on this INSERT
        # would be a redundant AND of the same predicate. bypass_scoping
        # keeps the emitted SQL exactly what we built and documents the
        # cross-bootstrap intent (ADR-014/015 + D-014).
        # --------------------------------------------------------------
        from ..db.models import Transcription

        with bypass_scoping(db):
            row = Transcription(
                user_id=user_id,
                audio_hash=audio_hash,
                original_filename=original_filename,
                original_size_bytes=original_size_bytes,
                duration_seconds=Decimal(str(payload["duration_seconds"])),
                language=payload["language"],
                num_speakers=payload["num_speakers"],
                text_content=payload["text_content"],
                segments={"segments": payload["segments"]},
                extra_metadata=payload["metadata"],
            )
            db.add(row)
            await db.flush()

        return {
            "transcription_id": row.id,
            **payload,
        }
    finally:
        # AC-1 cleanup invariant: regardless of outcome, the normalized
        # WAV does not linger on disk. The cache stores the JSON result
        # under cache_store's own base_dir, NOT under upload_dir.
        try:
            normalized_wav.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "normalized_wav_cleanup_failed path=%s error_id=NORMALIZED_WAV_LEAK",
                normalized_wav,
                exc_info=True,
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

    # CR-2: explicit `acquired` flag tracks whether THIS task owns the
    # lock. Replaces the prior `if _orchestrator_lock.locked(): release()`
    # defensive guard which read GLOBAL state and could incorrectly release
    # another task's hold.
    acquired = False
    try:
        await asyncio.wait_for(_orchestrator_lock.acquire(), timeout=lock_to)
        acquired = True
    except asyncio.TimeoutError as exc:
        # CR-1 (BPO-31647 mitigation): wait_for can raise TimeoutError
        # after the inner acquire() succeeded — leaving the lock held by
        # this task without `acquired` having been set. Detect via the
        # owner-task tracker built into _OrchestrationLock and release.
        if _orchestrator_lock.held_by_current_task():
            _orchestrator_lock.release()
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
        # AC-7 + CR-2: release based on the explicit `acquired` flag, NOT
        # on `_orchestrator_lock.locked()`. The latter is global state and
        # could be True due to ANOTHER task holding it — releasing in that
        # case would unlock theirs incorrectly.
        if acquired:
            _orchestrator_lock.release()
