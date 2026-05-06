"""Orchestrator global lock + GPUBusy + PipelineTimeout + lock-release.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-6 — Two concurrent `orchestrate(...)` calls into the same process MUST
  serialize. The first holds the module-level ``_orchestrator_lock``; the
  second waits up to ``lock_wait_seconds`` and then raises ``GPUBusy`` with
  ``retry_after = lock_retry_after_seconds``. ADR-005 (ONE GPU, one job at
  a time) is the rationale.
- AC-7 — When the inner pipeline raises (``GPUError``, ``PipelineDiarizeError``,
  generic ``Exception``), the lock is released in ``finally``. Tests assert
  ``not _orchestrator_lock.locked()`` post-error so a follow-up call can
  re-acquire immediately.
- AC-11 — When the inner pipeline takes longer than ``pipeline_timeout``
  seconds, ``PipelineTimeout(timeout_seconds=...)`` is raised AND the lock
  is released. Distinct from ``GPUBusy`` (which fires when the lock could
  NOT be acquired); ``PipelineTimeout`` fires AFTER acquiring the lock.

These tests patch ``transcription_api.pipeline.orchestrator._run_pipeline``
(the documented patchable seam, mirrors `_whisperx_load_model` and
`_pyannote_run_pipeline` from prior batches) so neither GPU nor DB nor
ffmpeg is needed. The DB session is a ``MagicMock`` for the same reason —
``_run_pipeline`` is the unit of work that touches the DB; with it
patched, the orchestrator wrapper is exercisable in isolation.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_orchestrate_kwargs(**overrides):
    """Build a minimal kwargs dict for `orchestrate(...)`.

    The orchestrator's signature includes whisper_model, pyannote_pipeline,
    cache_store, etc., but every test in this file patches `_run_pipeline`
    so those values are never dereferenced. They just need to be non-None
    so the wrapper passes its argument-validation step.
    """
    base = {
        "user_id": uuid.uuid4(),
        "db": MagicMock(name="async_session"),
        "file_path": Path("/tmp/x.mp3"),
        "original_filename": "x.mp3",
        "original_size_bytes": 1024,
        "whisper_model": MagicMock(name="whisper_model"),
        "pyannote_pipeline": MagicMock(name="pyannote_pipeline"),
        "cache_store": MagicMock(name="cache_store"),
        "upload_dir": Path("/tmp"),
        "language": "es",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# AC-6 — global lock serialization + GPUBusy timeout
# ---------------------------------------------------------------------------
async def test_orchestrator_serializes_two_concurrent_jobs_with_gpu_busy():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-6 — Given two concurrent ``orchestrate`` calls and
    ``lock_timeout=0.2`` on the second, When the first holds the lock by
    sleeping inside the inner pipeline, Then the second raises
    ``GPUBusy(retry_after=...)`` and the first eventually completes
    successfully.
    """
    from transcription_api.pipeline.orchestrator import GPUBusy, orchestrate

    async def _slow_inner(*args, **kwargs):
        await asyncio.sleep(0.5)
        return {"sentinel": "first"}

    with patch(
        "transcription_api.pipeline.orchestrator._run_pipeline",
        side_effect=_slow_inner,
    ):
        first = asyncio.create_task(
            orchestrate(**_make_orchestrate_kwargs(lock_timeout=5.0))
        )
        # Yield control so `first` reaches the lock acquisition before the
        # second call starts. Non-deterministic without this — the order of
        # asyncio.create_task scheduling is implementation-defined.
        await asyncio.sleep(0.05)

        with pytest.raises(GPUBusy) as exc:
            await orchestrate(
                **_make_orchestrate_kwargs(lock_timeout=0.2, retry_after=600)
            )
        assert exc.value.retry_after == 600

        result = await first
        assert result == {"sentinel": "first"}


async def test_orchestrator_lock_released_after_happy_completion():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-6 (lock release happy path) — Given a successful
    ``orchestrate`` call, When the call returns, Then the lock is
    released so the next call can re-acquire immediately.
    """
    from transcription_api.pipeline.orchestrator import (
        _orchestrator_lock,
        orchestrate,
    )

    async def _fast(*args, **kwargs):
        return {"sentinel": "ok"}

    with patch(
        "transcription_api.pipeline.orchestrator._run_pipeline",
        side_effect=_fast,
    ):
        await orchestrate(**_make_orchestrate_kwargs())

    assert not _orchestrator_lock.locked()


# ---------------------------------------------------------------------------
# AC-7 — lock released on inner-pipeline error
# ---------------------------------------------------------------------------
async def test_orchestrator_releases_lock_when_inner_raises_gpu_error():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-7 — Given the inner pipeline raises ``GPUError``
    (CUDA OOM remapped from STT), When ``orchestrate`` propagates the
    error, Then the lock is released in ``finally`` so the next call
    is not blocked by a leaked lock.
    """
    from transcription_api.pipeline.orchestrator import (
        _orchestrator_lock,
        orchestrate,
    )
    from transcription_api.pipeline.stt import GPUError

    async def _boom(*args, **kwargs):
        raise GPUError("oom", "CUDA out of memory")

    with patch(
        "transcription_api.pipeline.orchestrator._run_pipeline",
        side_effect=_boom,
    ):
        with pytest.raises(GPUError):
            await orchestrate(**_make_orchestrate_kwargs())

    assert not _orchestrator_lock.locked()


async def test_orchestrator_releases_lock_when_inner_raises_diarize_error():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-7 — Given the inner pipeline raises
    ``PipelineDiarizeError`` (pyannote runtime fault), When orchestrate
    propagates, Then the lock is released. Verifies the finally block
    catches all typed pipeline errors, not just GPUError.
    """
    from transcription_api.pipeline.diarize import PipelineDiarizeError
    from transcription_api.pipeline.orchestrator import (
        _orchestrator_lock,
        orchestrate,
    )

    async def _boom(*args, **kwargs):
        raise PipelineDiarizeError("pyannote internal failure")

    with patch(
        "transcription_api.pipeline.orchestrator._run_pipeline",
        side_effect=_boom,
    ):
        with pytest.raises(PipelineDiarizeError):
            await orchestrate(**_make_orchestrate_kwargs())

    assert not _orchestrator_lock.locked()


async def test_orchestrator_releases_lock_on_unexpected_exception():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-7 (defensive) — Given an unexpected ``RuntimeError``
    not in the spec catalog, When orchestrate propagates, Then the lock
    is STILL released. The ``finally`` block must not be selective.
    """
    from transcription_api.pipeline.orchestrator import (
        _orchestrator_lock,
        orchestrate,
    )

    async def _boom(*args, **kwargs):
        raise RuntimeError("unexpected non-typed failure")

    with patch(
        "transcription_api.pipeline.orchestrator._run_pipeline",
        side_effect=_boom,
    ):
        with pytest.raises(RuntimeError):
            await orchestrate(**_make_orchestrate_kwargs())

    assert not _orchestrator_lock.locked()


# ---------------------------------------------------------------------------
# AC-11 — pipeline timeout (lock acquired, inner work timed out)
# ---------------------------------------------------------------------------
async def test_orchestrator_pipeline_timeout_raises_typed_error():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-11 — Given the inner pipeline takes longer than
    ``pipeline_timeout``, When orchestrate is invoked, Then
    ``PipelineTimeout(timeout_seconds=...)`` is raised. The lock IS
    released (a successful acquisition followed by an inner timeout
    must not leak the lock).
    """
    from transcription_api.pipeline.orchestrator import (
        PipelineTimeout,
        _orchestrator_lock,
        orchestrate,
    )

    async def _hangs(*args, **kwargs):
        await asyncio.sleep(2.0)
        return {"never": "reached"}

    with patch(
        "transcription_api.pipeline.orchestrator._run_pipeline",
        side_effect=_hangs,
    ):
        with pytest.raises(PipelineTimeout) as exc:
            await orchestrate(
                **_make_orchestrate_kwargs(pipeline_timeout=0.1)
            )
        assert exc.value.timeout_seconds == 0.1

    assert not _orchestrator_lock.locked()


async def test_pipeline_timeout_is_distinct_from_gpu_busy():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-6 vs AC-11 (semantics) — ``GPUBusy`` and
    ``PipelineTimeout`` are distinct exception types so the API layer
    (Batch 6) can map them to different HTTP responses (503 with
    Retry-After for the former, 504 for the latter). Inheriting from
    a common base is fine, but they must not be the same class.
    """
    from transcription_api.pipeline.orchestrator import (
        GPUBusy,
        PipelineTimeout,
    )

    assert GPUBusy is not PipelineTimeout
    # Also verify neither is a subclass of the other in either direction.
    assert not issubclass(GPUBusy, PipelineTimeout)
    assert not issubclass(PipelineTimeout, GPUBusy)
