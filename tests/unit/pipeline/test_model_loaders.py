"""Model loaders for Whisper (STT) and pyannote (diarization).

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-9 — `load_whisper_model` returns an object exposing `.transcribe()`.
- AC-15 — `load_pyannote_pipeline` classifies HuggingFace token failures into
  a typed `PyannoteLoadError(detail=...)` so the lifespan can record the
  reason in `app.state.pyannote_detail` and propagate it to /health and to
  the 503 body of POST /api/transcriptions without bringing the service down
  (D-028: lazy + verbose).

Heavy imports (`whisperx`, `pyannote.audio`, `torch`) live inside the loader
functions, not at module top, so the unit suite can run on CPU machines that
do not have the `[pipeline]` extras installed. These tests patch the inner
indirection helpers (`_whisperx_load_model`, `_pyannote_from_pretrained`) so
no GPU or HuggingFace network access is required.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# AC-9 — Whisper loader
# ---------------------------------------------------------------------------
def test_load_whisper_returns_object_with_transcribe():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-9 — Given a configured (model_size, device, compute_type),
    When load_whisper_model is invoked, Then the returned object exposes a
    `.transcribe` callable. Heavy `whisperx.load_model` is patched.
    """
    from transcription_api.pipeline.stt import load_whisper_model

    with patch("transcription_api.pipeline.stt._whisperx_load_model") as m:
        m.return_value = MagicMock(transcribe=MagicMock())
        model = load_whisper_model("large-v3", "cuda", "int8_float16")

        assert hasattr(model, "transcribe")
        m.assert_called_once_with(
            "large-v3", device="cuda", compute_type="int8_float16"
        )


def test_load_whisper_propagates_runtime_errors():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-9 — Given the inner whisperx loader raises (e.g. CUDA driver
    missing), When load_whisper_model is invoked, Then the original exception
    propagates so the lifespan can record `app.state.whisper_status = "error"`
    with the original message as detail.
    """
    from transcription_api.pipeline.stt import load_whisper_model

    with patch("transcription_api.pipeline.stt._whisperx_load_model") as m:
        m.side_effect = RuntimeError("CUDA driver not available")
        with pytest.raises(RuntimeError, match="CUDA driver not available"):
            load_whisper_model("large-v3", "cuda", "int8_float16")


# ---------------------------------------------------------------------------
# AC-15 — pyannote loader with verbose error classification
# ---------------------------------------------------------------------------
def test_load_pyannote_classifies_empty_token_as_missing():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-15 — Given HF_TOKEN is empty, When load_pyannote_pipeline
    is invoked, Then PyannoteLoadError is raised with detail='hf_token_missing'
    BEFORE calling HuggingFace (no network round-trip needed to detect this).
    """
    from transcription_api.pipeline.diarize import (
        PyannoteLoadError,
        load_pyannote_pipeline,
    )

    with patch(
        "transcription_api.pipeline.diarize._pyannote_from_pretrained"
    ) as m:
        with pytest.raises(PyannoteLoadError) as exc:
            load_pyannote_pipeline("")
        assert exc.value.detail == "hf_token_missing"
        m.assert_not_called()


def test_load_pyannote_classifies_unauthorized_as_invalid_token():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-15 — Given HuggingFace returns 401 Unauthorized for the
    provided token, When load_pyannote_pipeline is invoked, Then
    PyannoteLoadError(detail='hf_token_invalid') is raised.
    """
    from transcription_api.pipeline.diarize import (
        PyannoteLoadError,
        load_pyannote_pipeline,
    )

    with patch(
        "transcription_api.pipeline.diarize._pyannote_from_pretrained"
    ) as m:
        m.side_effect = Exception("401 Client Error: Unauthorized for url: ...")
        with pytest.raises(PyannoteLoadError) as exc:
            load_pyannote_pipeline("hf_invalid_token_value")
        assert exc.value.detail == "hf_token_invalid"


def test_load_pyannote_classifies_gated_repo_as_terms_not_accepted():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-15 — Given HuggingFace replies that the repo is gated and
    terms must be accepted, When load_pyannote_pipeline is invoked, Then
    PyannoteLoadError(detail='hf_terms_not_accepted') is raised so the
    operator knows to accept the terms on huggingface.co.
    """
    from transcription_api.pipeline.diarize import (
        PyannoteLoadError,
        load_pyannote_pipeline,
    )

    with patch(
        "transcription_api.pipeline.diarize._pyannote_from_pretrained"
    ) as m:
        m.side_effect = Exception(
            "Cannot access gated repo for url ...; "
            "you must accept the terms of pyannote/speaker-diarization-3.1"
        )
        with pytest.raises(PyannoteLoadError) as exc:
            load_pyannote_pipeline("hf_token_pending_terms")
        assert exc.value.detail == "hf_terms_not_accepted"


def test_load_pyannote_classifies_unknown_failure():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-15 — Given an unrecognised HuggingFace failure (e.g. DNS
    error, 5xx), When load_pyannote_pipeline is invoked, Then
    PyannoteLoadError(detail='unknown') is raised so /health surfaces the
    error category without leaking the raw message into the public API.
    """
    from transcription_api.pipeline.diarize import (
        PyannoteLoadError,
        load_pyannote_pipeline,
    )

    with patch(
        "transcription_api.pipeline.diarize._pyannote_from_pretrained"
    ) as m:
        m.side_effect = ConnectionError("DNS resolution failed for huggingface.co")
        with pytest.raises(PyannoteLoadError) as exc:
            load_pyannote_pipeline("hf_token_value")
        assert exc.value.detail == "unknown"


def test_load_pyannote_returns_pipeline_on_success():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-9 — Given a valid HF token, When load_pyannote_pipeline is
    invoked, Then the underlying pyannote Pipeline instance is returned
    unchanged. Verifies the model_id default points at the spec-canonical
    `pyannote/speaker-diarization-3.1`.
    """
    from transcription_api.pipeline.diarize import load_pyannote_pipeline

    fake_pipeline = MagicMock(name="pyannote_pipeline")
    with patch(
        "transcription_api.pipeline.diarize._pyannote_from_pretrained"
    ) as m:
        m.return_value = fake_pipeline
        result = load_pyannote_pipeline("hf_valid_token")

        assert result is fake_pipeline
        m.assert_called_once_with(
            "pyannote/speaker-diarization-3.1", "hf_valid_token"
        )
