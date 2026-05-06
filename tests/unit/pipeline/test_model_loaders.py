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


def test_load_whisper_classifies_cuda_unavailable_runtime_errors():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-9 + H-5 — Given the inner whisperx loader raises with a
    message that signals CUDA unavailability, When load_whisper_model is
    invoked, Then the wrapper raises WhisperLoadError with detail
    `cuda_unavailable` so the lifespan can surface a stable discriminator
    on `/health.whisper_detail` and the API 503 body (mirrors the pyannote
    pattern).

    Capa 3 review H-5: previously this test asserted that the raw
    RuntimeError propagated. The current contract classifies failures
    into a closed set of detail strings; we assert that here.
    """
    from transcription_api.pipeline.stt import (
        DETAIL_LOAD_CUDA_UNAVAILABLE,
        WhisperLoadError,
        load_whisper_model,
    )

    with patch("transcription_api.pipeline.stt._whisperx_load_model") as m:
        m.side_effect = RuntimeError("CUDA driver not available; cuda is not available")
        with pytest.raises(WhisperLoadError) as exc_info:
            load_whisper_model("large-v3", "cuda", "int8_float16")
        assert exc_info.value.detail == DETAIL_LOAD_CUDA_UNAVAILABLE


def test_load_whisper_classifies_cuda_oom_at_load():
    """H-5: OOM during load classifies as `cuda_oom_at_load`."""
    from transcription_api.pipeline.stt import (
        DETAIL_LOAD_OOM,
        WhisperLoadError,
        load_whisper_model,
    )

    with patch("transcription_api.pipeline.stt._whisperx_load_model") as m:
        m.side_effect = RuntimeError("CUDA out of memory")
        with pytest.raises(WhisperLoadError) as exc_info:
            load_whisper_model("large-v3", "cuda", "int8_float16")
        assert exc_info.value.detail == DETAIL_LOAD_OOM


def test_load_whisper_classifies_model_not_found():
    """H-5: 404 / not-found / no-such-file → `model_not_found`."""
    from transcription_api.pipeline.stt import (
        DETAIL_LOAD_MODEL_NOT_FOUND,
        WhisperLoadError,
        load_whisper_model,
    )

    with patch("transcription_api.pipeline.stt._whisperx_load_model") as m:
        m.side_effect = OSError("No such file or directory: '/data/models/...'")
        with pytest.raises(WhisperLoadError) as exc_info:
            load_whisper_model("large-v3", "cuda", "int8_float16")
        assert exc_info.value.detail == DETAIL_LOAD_MODEL_NOT_FOUND


def test_load_whisper_unknown_error_classifies_as_unknown():
    """H-5: an exception message that doesn't match any heuristic falls back
    to the `unknown` discriminator (not silenced or coerced to a sibling)."""
    from transcription_api.pipeline.stt import (
        DETAIL_LOAD_UNKNOWN,
        WhisperLoadError,
        load_whisper_model,
    )

    with patch("transcription_api.pipeline.stt._whisperx_load_model") as m:
        m.side_effect = ValueError("malformed config blob")
        with pytest.raises(WhisperLoadError) as exc_info:
            load_whisper_model("large-v3", "cuda", "int8_float16")
        assert exc_info.value.detail == DETAIL_LOAD_UNKNOWN


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


# ---------------------------------------------------------------------------
# Capa 3 review post-rig — pyannote.audio kwarg API change.
# These tests inject a fake `pyannote.audio` module into sys.modules so
# they run without `[pipeline]` extras installed (CPU dev box).
# ---------------------------------------------------------------------------
def _install_fake_pyannote(monkeypatch, mock_from_pretrained):
    """Wire a fake `pyannote.audio.Pipeline.from_pretrained` into sys.modules.

    Returns nothing; the patch is undone when monkeypatch tears down.
    """
    import sys
    import types

    fake_pipeline_cls = MagicMock()
    fake_pipeline_cls.from_pretrained = mock_from_pretrained

    fake_audio_mod = types.ModuleType("pyannote.audio")
    fake_audio_mod.Pipeline = fake_pipeline_cls  # type: ignore[attr-defined]

    fake_pyannote_mod = types.ModuleType("pyannote")
    fake_pyannote_mod.audio = fake_audio_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "pyannote", fake_pyannote_mod)
    monkeypatch.setitem(sys.modules, "pyannote.audio", fake_audio_mod)


def test_pyannote_from_pretrained_uses_token_kwarg_first(monkeypatch):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: post-rig fix — pyannote.audio 4.x renamed `use_auth_token=`
    to `token=`. The wrapper MUST try `token=` first so deployments
    against the latest pyannote work without a TypeError.
    """
    fake = MagicMock(name="pyannote_pipeline_instance")
    mock_from_pretrained = MagicMock(return_value=fake)
    _install_fake_pyannote(monkeypatch, mock_from_pretrained)

    from transcription_api.pipeline.diarize import _pyannote_from_pretrained

    result = _pyannote_from_pretrained("model_id", "hf_token_value")

    assert result is fake
    mock_from_pretrained.assert_called_once_with(
        "model_id", token="hf_token_value"
    )


def test_pyannote_from_pretrained_falls_back_to_use_auth_token(monkeypatch):
    """Post-rig fix — if the installed pyannote rejects ``token=`` (3.x),
    the wrapper falls back to the legacy ``use_auth_token=`` kwarg so
    older pyannote installations keep working."""
    fake = MagicMock(name="pyannote_pipeline_legacy")

    def fake_from_pretrained(model_id, **kwargs):
        if "token" in kwargs and "use_auth_token" not in kwargs:
            raise TypeError(
                "Pipeline.from_pretrained() got an unexpected keyword argument 'token'"
            )
        if "use_auth_token" in kwargs:
            return fake
        raise AssertionError(f"unexpected kwargs: {kwargs}")

    mock_from_pretrained = MagicMock(side_effect=fake_from_pretrained)
    _install_fake_pyannote(monkeypatch, mock_from_pretrained)

    from transcription_api.pipeline.diarize import _pyannote_from_pretrained

    result = _pyannote_from_pretrained("model_id", "hf_token_value")

    assert result is fake
    # Two calls: first with token=, second with use_auth_token=.
    assert mock_from_pretrained.call_count == 2
    assert mock_from_pretrained.call_args_list[0].kwargs == {"token": "hf_token_value"}
    assert mock_from_pretrained.call_args_list[1].kwargs == {
        "use_auth_token": "hf_token_value"
    }


def test_pyannote_from_pretrained_propagates_unrelated_typeerror(monkeypatch):
    """A TypeError that is NOT about the token kwarg must propagate as-is
    so a real bug isn't silently swallowed by the fallback path."""
    mock_from_pretrained = MagicMock(
        side_effect=TypeError("model_id must be a string")
    )
    _install_fake_pyannote(monkeypatch, mock_from_pretrained)

    from transcription_api.pipeline.diarize import _pyannote_from_pretrained

    with pytest.raises(TypeError, match="model_id must be a string"):
        _pyannote_from_pretrained(123, "hf_token_value")  # type: ignore[arg-type]
