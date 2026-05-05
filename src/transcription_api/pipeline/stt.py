"""WhisperX speech-to-text loader.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-9 — `load_whisper_model` returns an object with `.transcribe()` after
  startup, exposed to /health via `app.state.whisper_status = "ready"`.

The whisperx import is delayed until the loader is called so the package is
importable on CPU-only machines without the `[pipeline]` extras. Tests patch
the indirection helper `_whisperx_load_model` rather than `whisperx.load_model`
directly because the latter would require the extras to even patch.
"""
from __future__ import annotations

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
