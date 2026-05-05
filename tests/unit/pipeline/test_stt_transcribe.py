"""WhisperX inference wrapper.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-1 (STT side) — `transcribe(model, audio_path, ...)` is the thin wrapper
  the orchestrator (Batch 5) calls between normalize and diarize. It must
  forward the WhisperX-canonical shape ``{"segments": [...], "language": "es"}``
  unchanged so downstream code can read both the per-word timing and the
  detected language without paying a second pass through the audio.
- AC-7 (STT side) — CUDA OOM / RuntimeError / CUBLAS errors raised by
  ``model.transcribe()`` are remapped to ``GPUError(detail=...)`` so the
  API layer (Batch 6) can produce 500 ``GPU_ERROR`` with a stable detail
  while the orchestrator (Batch 5) releases the GPU lock in ``finally``.

Tests run on CPU dev machines — torch/whisperx are NOT installed in
``.venv``. The ``model`` is a ``MagicMock`` whose ``.transcribe`` is
configured to either return a canonical dict or ``side_effect`` a
synthetic CUDA error. Class-name matching (not ``isinstance``) lets us
simulate ``torch.cuda.OutOfMemoryError`` without importing torch.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Canonical WhisperX response shape — used across the file.
# ---------------------------------------------------------------------------
_CANONICAL_RESULT = {
    "segments": [
        {
            "start": 0.0,
            "end": 1.0,
            "text": "Hola",
            "words": [
                {"word": "Hola", "start": 0.0, "end": 1.0, "score": 0.99},
            ],
        },
        {
            "start": 1.0,
            "end": 2.0,
            "text": "soy yo",
            "words": [
                {"word": "soy", "start": 1.0, "end": 1.5, "score": 0.98},
                {"word": "yo", "start": 1.5, "end": 2.0, "score": 0.97},
            ],
        },
    ],
    "language": "es",
}


# ---------------------------------------------------------------------------
# AC-1 — happy path passthrough
# ---------------------------------------------------------------------------
def test_transcribe_returns_canonical_whisperx_shape():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given a model whose `transcribe` returns the
    WhisperX-canonical dict ``{"segments": [...], "language": "es"}``,
    When `pipeline.stt.transcribe` is invoked, Then the dict is returned
    unchanged so the orchestrator can preserve both timing and detected
    language for the response body (`metadata.language`).
    """
    from transcription_api.pipeline.stt import transcribe

    model = MagicMock()
    model.transcribe.return_value = _CANONICAL_RESULT

    out = transcribe(model, Path("/tmp/x.wav"))

    assert out is _CANONICAL_RESULT or out == _CANONICAL_RESULT
    assert "segments" in out
    assert "language" in out
    assert len(out["segments"]) == 2
    assert out["segments"][0]["words"][0]["word"] == "Hola"


def test_transcribe_passes_audio_path_as_string():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given a `pathlib.Path` audio_path, When transcribe
    forwards to the underlying model, Then the path is coerced to ``str``.
    WhisperX expects a string (it shells out to ffmpeg internally and
    `Path` would propagate as-is into a subprocess argument that may
    or may not accept it depending on the version).
    """
    from transcription_api.pipeline.stt import transcribe

    model = MagicMock()
    model.transcribe.return_value = _CANONICAL_RESULT

    transcribe(model, Path("/tmp/x.wav"))

    args, kwargs = model.transcribe.call_args
    # Whether the impl uses positional or keyword for the audio path is
    # an implementation detail; verify the value made it through as str.
    forwarded = args[0] if args else kwargs.get("audio")
    assert isinstance(forwarded, str)
    assert forwarded == "/tmp/x.wav"


def test_transcribe_forwards_language_hint():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given an explicit `language="es"`, When transcribe
    forwards to the underlying model, Then ``language`` is passed through
    as a keyword arg so WhisperX skips its language-detection pass and
    the user-provided hint wins.
    """
    from transcription_api.pipeline.stt import transcribe

    model = MagicMock()
    model.transcribe.return_value = _CANONICAL_RESULT

    transcribe(model, Path("/tmp/x.wav"), language="es")

    _, kwargs = model.transcribe.call_args
    assert kwargs.get("language") == "es"


def test_transcribe_omits_language_when_not_provided():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given no `language` argument (None default), When
    transcribe forwards to the model, Then no ``language`` keyword is
    passed (lets WhisperX auto-detect). Avoids passing ``language=None``
    which some WhisperX versions misinterpret as the literal language
    code "None".
    """
    from transcription_api.pipeline.stt import transcribe

    model = MagicMock()
    model.transcribe.return_value = _CANONICAL_RESULT

    transcribe(model, Path("/tmp/x.wav"))

    _, kwargs = model.transcribe.call_args
    assert "language" not in kwargs or kwargs.get("language") is not None


def test_transcribe_forwards_batch_size_default_8():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 + ADR-001 budget — Given no explicit batch_size, When
    transcribe forwards to the model, Then ``batch_size=8`` is passed
    (default for the 8 GB VRAM rig per spec §0.3 + WHISPER_BATCH_SIZE).
    """
    from transcription_api.pipeline.stt import transcribe

    model = MagicMock()
    model.transcribe.return_value = _CANONICAL_RESULT

    transcribe(model, Path("/tmp/x.wav"))

    _, kwargs = model.transcribe.call_args
    assert kwargs.get("batch_size") == 8


def test_transcribe_forwards_explicit_batch_size():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given an explicit `batch_size=4` (the documented
    fallback for OOM, spec §0.3), When transcribe forwards to the model,
    Then the value is passed as kwarg (no silent override to the default).
    """
    from transcription_api.pipeline.stt import transcribe

    model = MagicMock()
    model.transcribe.return_value = _CANONICAL_RESULT

    transcribe(model, Path("/tmp/x.wav"), batch_size=4)

    _, kwargs = model.transcribe.call_args
    assert kwargs.get("batch_size") == 4


# ---------------------------------------------------------------------------
# AC-7 — CUDA error mapping
# ---------------------------------------------------------------------------
# Synthetic OOM type — name matches torch.cuda.OutOfMemoryError so the
# class-name discriminator in stt.transcribe matches without importing
# torch (CPU-only dev box, D-008/D-030).
_SyntheticOutOfMemoryError = type("OutOfMemoryError", (RuntimeError,), {})


def test_transcribe_maps_cuda_oom_to_gpu_error():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-7 — Given model.transcribe raises an OutOfMemoryError
    (name-matched to torch.cuda.OutOfMemoryError), When transcribe is
    invoked, Then GPUError is raised with detail='oom' so the orchestrator
    (Batch 5) releases the GPU lock and the API (Batch 6) returns 500
    GPU_ERROR with a stable detail.
    """
    from transcription_api.pipeline.stt import GPUError, transcribe

    model = MagicMock()
    model.transcribe.side_effect = _SyntheticOutOfMemoryError(
        "CUDA out of memory. Tried to allocate 2.00 GiB"
    )

    with pytest.raises(GPUError) as exc:
        transcribe(model, Path("/tmp/x.wav"))
    assert exc.value.detail == "oom"


def test_transcribe_maps_cuda_runtime_error_to_gpu_error():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-7 — Given model.transcribe raises RuntimeError whose
    message cites CUDA (e.g. 'CUDA error: invalid argument'), When
    transcribe is invoked, Then GPUError(detail='runtime') is raised.
    """
    from transcription_api.pipeline.stt import GPUError, transcribe

    model = MagicMock()
    model.transcribe.side_effect = RuntimeError("CUDA error: invalid argument")

    with pytest.raises(GPUError) as exc:
        transcribe(model, Path("/tmp/x.wav"))
    assert exc.value.detail == "runtime"


def test_transcribe_maps_cublas_runtime_error_to_gpu_error():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-7 — Given model.transcribe raises RuntimeError whose
    message cites CUBLAS (cuBLAS BLAS library failure), When transcribe
    is invoked, Then GPUError(detail='runtime') is raised. CUBLAS errors
    surface from int8 quantized inference under memory pressure.
    """
    from transcription_api.pipeline.stt import GPUError, transcribe

    model = MagicMock()
    model.transcribe.side_effect = RuntimeError(
        "CUBLAS_STATUS_EXECUTION_FAILED when calling `cublasGemmEx(...)`"
    )

    with pytest.raises(GPUError) as exc:
        transcribe(model, Path("/tmp/x.wav"))
    assert exc.value.detail == "runtime"


def test_transcribe_does_not_remap_non_gpu_runtime_errors():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-7 (defensive) — Given model.transcribe raises a
    RuntimeError whose message has nothing to do with CUDA/CUBLAS (e.g.
    a code bug in WhisperX or a malformed config), When transcribe is
    invoked, Then the original RuntimeError propagates UNCHANGED. We
    don't want to masquerade non-GPU faults as GPUError because the
    orchestrator's recovery path (release lock, retry advice) only
    makes sense for actual GPU faults.
    """
    from transcription_api.pipeline.stt import GPUError, transcribe

    model = MagicMock()
    model.transcribe.side_effect = RuntimeError("some unrelated runtime bug")

    with pytest.raises(RuntimeError) as exc:
        transcribe(model, Path("/tmp/x.wav"))
    assert not isinstance(exc.value, GPUError)
    assert "unrelated runtime bug" in str(exc.value)


def test_transcribe_does_not_remap_non_runtime_exceptions():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-7 (defensive) — Given model.transcribe raises a
    non-RuntimeError (e.g. ValueError from a malformed audio path),
    When transcribe is invoked, Then the exception propagates unchanged.
    GPUError is reserved for GPU faults.
    """
    from transcription_api.pipeline.stt import GPUError, transcribe

    model = MagicMock()
    model.transcribe.side_effect = ValueError("not a valid audio file")

    with pytest.raises(ValueError):
        transcribe(model, Path("/tmp/x.wav"))
    assert not model.transcribe.side_effect.__class__.__name__ == GPUError.__name__
