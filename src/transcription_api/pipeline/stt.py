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


# ---------------------------------------------------------------------------
# AC-7 — CUDA error mapping
# ---------------------------------------------------------------------------
# Stable discriminators surfaced as ``GPUError.detail``. The API layer
# (Batch 6) maps GPUError to HTTP 500 ``GPU_ERROR`` and copies the detail
# into the response body so operators can distinguish memory pressure
# (smaller batch_size or smaller model) from a generic CUDA fault
# (driver / hardware level).
DETAIL_OOM = "oom"
DETAIL_RUNTIME = "runtime"


class GPUError(Exception):
    """Raised when WhisperX inference fails due to a GPU-level fault.

    ``detail`` is one of ``DETAIL_*`` and is the only piece of the error
    that escapes to the public API surface. The upstream message is kept
    in ``args[0]`` for log inspection.
    """

    def __init__(self, detail: str, message: str = "") -> None:
        self.detail = detail
        super().__init__(message or detail)


def _is_cuda_oom(exc: BaseException) -> bool:
    """Return True if ``exc`` represents a CUDA out-of-memory condition.

    We match by class name (``OutOfMemoryError``) AND by message text
    rather than ``isinstance(exc, torch.cuda.OutOfMemoryError)``: the
    real torch type only exists when torch is installed, which is NOT
    the case on CPU-only dev machines (D-008 / D-030). Tests synthesize
    the type with the same name; production raises the real one — both
    cases match without importing torch here.
    """
    if type(exc).__name__ == "OutOfMemoryError":
        return True
    msg = str(exc).lower()
    return "out of memory" in msg or "cuda oom" in msg


def _is_cuda_runtime_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is a CUDA / CUBLAS runtime fault.

    Pattern-match the message — the real type is ``RuntimeError`` so
    ``isinstance`` would not discriminate from ordinary application
    RuntimeErrors. Conservative: only match when the message explicitly
    cites CUDA or CUBLAS so non-GPU RuntimeErrors propagate untouched.
    """
    if not isinstance(exc, RuntimeError):
        return False
    msg = str(exc).lower()
    return "cuda" in msg or "cublas" in msg


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

    Maps CUDA OOM and CUDA/CUBLAS RuntimeErrors to ``GPUError`` so the
    orchestrator can release the GPU lock in ``finally`` and the API
    layer can return 500 ``GPU_ERROR`` with a stable detail. Other
    exceptions propagate untouched.
    """
    kwargs: dict[str, Any] = {"batch_size": batch_size}
    if language is not None:
        kwargs["language"] = language

    try:
        return model.transcribe(str(audio_path), **kwargs)
    except Exception as exc:
        if _is_cuda_oom(exc):
            raise GPUError(DETAIL_OOM, str(exc)) from exc
        if _is_cuda_runtime_error(exc):
            raise GPUError(DETAIL_RUNTIME, str(exc)) from exc
        raise
