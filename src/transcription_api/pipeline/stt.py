"""WhisperX speech-to-text loader and inference wrapper.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-9  — `load_whisper_model` returns an object with `.transcribe()` after
  startup, exposed to /health via `app.state.whisper_status = "ready"`.
- AC-1  — `transcribe(model, audio_path, ...)` is the orchestrator-facing
  inference wrapper. Returns the canonical WhisperX dict
  ``{"segments": [...], "language": "..."}`` unchanged (D-034: kept the
  upstream shape rather than the plan's proposed bare list, so the
  orchestrator preserves both timing and detected language).
- AC-7  — CUDA OOM / RuntimeError / CUBLAS errors are remapped to
  ``GPUError(detail=...)``. The mapping uses class-name + message matching
  so the module imports cleanly on CPU-only dev machines (no torch).

The whisperx import is delayed until the loader is called so the package is
importable on CPU-only machines without the `[pipeline]` extras. Tests patch
the indirection helper `_whisperx_load_model` rather than `whisperx.load_model`
directly because the latter would require the extras to even patch.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _whisperx_load_model(
    model_size: str, *, device: str, compute_type: str
) -> Any:
    """Indirection over `whisperx.load_model` for patchability in unit tests.

    The import lives inside the function so importing `transcription_api.pipeline.stt`
    from a CPU-only environment (no `[pipeline]` extras) succeeds; only invoking
    the loader requires whisperx to be present.
    """
    import whisperx

    return whisperx.load_model(model_size, device=device, compute_type=compute_type)


def load_whisper_model(model_size: str, device: str, compute_type: str) -> Any:
    """Load a WhisperX inference model.

    Args:
        model_size: faster-whisper model id, e.g. ``large-v3``.
        device: ``cuda`` on the rig, ``cpu`` for fallback dev.
        compute_type: quantization, e.g. ``int8_float16`` for the 8 GB VRAM
            budget on the rig (ADR-001 + D-001).

    Returns:
        The whisperx model handle. Caller stores it on ``app.state.whisper_model``.

    Raises:
        Whatever whisperx raises (CUDA driver missing, OOM at load, etc.).
        The lifespan catches generically and records the message in
        ``app.state.whisper_detail`` so /health surfaces the failure
        without aborting the service.
    """
    return _whisperx_load_model(
        model_size, device=device, compute_type=compute_type
    )


def transcribe(
    model: Any,
    audio_path: Path | str,
    *,
    language: str | None = None,
    batch_size: int = 8,
) -> dict[str, Any]:
    """Run WhisperX inference and return the canonical result dict.

    Thin wrapper over ``model.transcribe(...)``: passes ``audio_path`` as
    a string (WhisperX shells out to ffmpeg internally and is finicky
    about Path objects on some versions), forwards the optional language
    hint, and pins ``batch_size`` to the spec-canonical default of 8 for
    the 8 GB VRAM budget (ADR-001 + spec §0.3).

    Returns the upstream ``{"segments": [...], "language": "..."}`` dict
    unchanged (D-034). The orchestrator (Batch 5) consumes both keys when
    building the API response.

    AC-7 GPU error mapping is added in T3.2 (next task in this batch);
    this thin wrapper currently lets all exceptions propagate so T3.2's
    RED phase has something to fail against.
    """
    kwargs: dict[str, Any] = {"batch_size": batch_size}
    if language is not None:
        kwargs["language"] = language
    return model.transcribe(str(audio_path), **kwargs)
