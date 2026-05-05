"""pyannote diarization loader with verbose error classification.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-9  — `load_pyannote_pipeline` returns a usable Pipeline on success and
  the lifespan records `app.state.pyannote_status = "ready"`.
- AC-15 — On HuggingFace failure (token missing, token invalid, gated repo,
  network) the loader raises `PyannoteLoadError` with a `detail`
  discriminator. The lifespan stores the discriminator in
  `app.state.pyannote_detail`; /health echoes it as `models.pyannote_detail`,
  and `POST /api/transcriptions` returns 503 `MODELS_NOT_LOADED` with the
  same detail propagated. Service stays UP (D-028: lazy + verbose).

The pyannote import is delayed for the same reason described in `stt.py`:
CPU-only machines without the `[pipeline]` extras must still be able to
import this module so the package surface is testable via mocks.
"""
from __future__ import annotations

from typing import Any

# Discriminators surfaced to /health and to the 503 body of POST. Stable
# string contract — tests assert on these literals (AC-15).
DETAIL_TOKEN_MISSING = "hf_token_missing"
DETAIL_TOKEN_INVALID = "hf_token_invalid"
DETAIL_TERMS_NOT_ACCEPTED = "hf_terms_not_accepted"
DETAIL_UNKNOWN = "unknown"


class PyannoteLoadError(Exception):
    """Raised when the pyannote pipeline cannot be loaded.

    `detail` is one of the ``DETAIL_*`` constants and is the only piece of
    the error that escapes to the public API surface; the upstream message
    is kept in ``args[0]`` for log inspection but is not echoed to clients.
    """

    def __init__(self, detail: str, message: str = "") -> None:
        self.detail = detail
        super().__init__(message or detail)


def _pyannote_from_pretrained(model_id: str, hf_token: str) -> Any:
    """Indirection over `pyannote.audio.Pipeline.from_pretrained`.

    Imported inside the function so the module is importable on CPU-only
    machines without the `[pipeline]` extras (mirrors `stt._whisperx_load_model`).
    """
    from pyannote.audio import Pipeline

    return Pipeline.from_pretrained(model_id, use_auth_token=hf_token)


def load_pyannote_pipeline(
    hf_token: str,
    model_id: str = "pyannote/speaker-diarization-3.1",
) -> Any:
    """Load a pyannote Pipeline by HuggingFace model id.

    Maps known HuggingFace failure modes to typed ``PyannoteLoadError``
    instances so the lifespan can record a stable discriminator in
    ``app.state.pyannote_detail``. Empty tokens short-circuit before any
    network call (AC-15: no need to consult HF to know the token is missing).
    """
    if not hf_token:
        raise PyannoteLoadError(
            DETAIL_TOKEN_MISSING,
            "HF_TOKEN is empty; pyannote requires a HuggingFace access token "
            "with the speaker-diarization-3.1 terms accepted.",
        )

    try:
        return _pyannote_from_pretrained(model_id, hf_token)
    except PyannoteLoadError:
        raise
    except Exception as exc:
        message = str(exc).lower()
        if "401" in message or "unauthorized" in message:
            raise PyannoteLoadError(DETAIL_TOKEN_INVALID, str(exc)) from exc
        if (
            "gated" in message
            or "accept the terms" in message
            or "403" in message
        ):
            raise PyannoteLoadError(DETAIL_TERMS_NOT_ACCEPTED, str(exc)) from exc
        raise PyannoteLoadError(DETAIL_UNKNOWN, str(exc)) from exc
