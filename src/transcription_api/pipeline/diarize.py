"""pyannote diarization loader and inference wrapper.

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
- AC-1  — `diarize(pipeline_obj, wav_path, ...)` is the orchestrator-facing
  inference wrapper that returns ``list[(start, end, speaker)]`` tuples
  consumed by ``pipeline.merge.assign_speakers_to_words`` (Batch 4 task 4.2).
- ALT-3 — When pyannote returns more unique speakers than ``max_speakers``,
  the wrapper caps the result by relabeling the smallest-duration speakers
  to the temporally-closest top-N speaker (cap-by-relabel: one-pass, no
  pipeline re-run, see D-035).
- error catalog ``PIPELINE_DIARIZE_ERROR`` — pyannote runtime faults are
  mapped to ``PipelineDiarizeError`` so the orchestrator (Batch 5) can
  release the GPU lock and the API layer (Batch 6) can return HTTP 500.

The pyannote import is delayed for the same reason described in `stt.py`:
CPU-only machines without the `[pipeline]` extras must still be able to
import this module so the package surface is testable via mocks.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
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

    Capa 3 review post-rig: pyannote.audio 4.x renamed the auth kwarg from
    ``use_auth_token=`` (3.x) to ``token=`` (HF Hub convention). Try the
    new signature first; fall back to the legacy one if the installed
    pyannote happens to be 3.x. The fallback is forward-compat and keeps
    the function robust against pip resolving either major version.
    """
    from pyannote.audio import Pipeline

    try:
        return Pipeline.from_pretrained(model_id, token=hf_token)
    except TypeError as exc:
        # Only retry on the specific "unexpected keyword argument 'token'"
        # case; any other TypeError (e.g., wrong model id type) propagates.
        if "token" not in str(exc):
            raise
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


# ---------------------------------------------------------------------------
# AC-1 + ALT-3 — diarize wrapper
# ---------------------------------------------------------------------------
class PipelineDiarizeError(Exception):
    """Raised when the pyannote pipeline fails at inference time.

    Distinct from ``PyannoteLoadError`` (which fires at startup if the model
    can't be loaded). The API layer (Batch 6) maps this to HTTP 500
    ``PIPELINE_DIARIZE_ERROR`` per the spec error catalog.
    """


def _pyannote_run_pipeline(
    pipeline_obj: Any,
    wav_path: str,
    *,
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
) -> list[tuple[float, float, str]]:
    """Run the pyannote pipeline on ``wav_path`` and yield RTTM-shaped tuples.

    Indirection level so unit tests can patch this out and exercise the
    surrounding wrapper logic (forwarding kwargs, ALT-3 cap, error mapping)
    without pyannote.audio installed. Mirrors the ``_whisperx_load_model``
    pattern from Batch 1.

    The conversion from pyannote's ``Annotation`` to a plain list of tuples
    happens here so downstream code (orchestrator, merge) does not import
    pyannote types.
    """
    kwargs: dict[str, Any] = {}
    if num_speakers is not None:
        kwargs["num_speakers"] = num_speakers
    if min_speakers is not None:
        kwargs["min_speakers"] = min_speakers
    if max_speakers is not None:
        kwargs["max_speakers"] = max_speakers

    # Pyannote 3.x defers audio decoding to torchcodec by default. On the
    # rig (torch 2.8 + cuda 12.8) torchcodec's libavutil mismatch makes
    # the AudioDecoder import fail silently at boot (logged as warning)
    # and surface at inference time as ``NameError: name 'AudioDecoder'
    # is not defined``. Pre-loading via torchaudio (SoX/libsndfile, no
    # torchcodec) and passing the in-memory dict that pyannote documents
    # as a supported input bypasses the broken decoder entirely. Cost
    # is ~10 MB VRAM per 5 min of mono 16 kHz audio — negligible vs.
    # the model footprint, and the WAV is already mono 16 kHz from
    # ``normalize_audio`` so no resampling happens.
    import torchaudio  # noqa: PLC0415 — lazy: keep test envs lighter

    waveform, sample_rate = torchaudio.load(wav_path)
    audio_input = {"waveform": waveform, "sample_rate": sample_rate}

    output = pipeline_obj(audio_input, **kwargs)

    # Pyannote 3.5+ wraps the diarization Annotation in a ``DiarizeOutput``
    # dataclass when called with in-memory audio (returns
    # ``DiarizeOutput(speaker_diarization=..., embeddings=...)``).
    # Earlier versions and the path-based legacy call return the
    # Annotation directly. Handle both shapes defensively — the field
    # name is documented as ``speaker_diarization`` in the 3.5 release
    # notes; ``diarization`` is the older alias kept for compatibility.
    annotation = (
        getattr(output, "speaker_diarization", None)
        or getattr(output, "diarization", None)
        or output
    )

    if not hasattr(annotation, "itertracks"):
        raise PipelineDiarizeError(
            f"pyannote returned unexpected type {type(annotation).__name__}; "
            "expected pyannote.core.Annotation or a wrapper exposing it"
        )

    # itertracks(yield_label=True) yields (segment, track_id, speaker_label).
    return [
        (float(seg.start), float(seg.end), str(label))
        for seg, _track, label in annotation.itertracks(yield_label=True)
    ]


def _cap_speakers_by_duration(
    segments: list[tuple[float, float, str]], max_speakers: int
) -> list[tuple[float, float, str]]:
    """Cap unique speakers to ``max_speakers`` by relabel (ALT-3, D-035).

    Algorithm:
    1. Sum total duration per speaker.
    2. Keep the ``max_speakers`` highest-duration speakers ("top-N").
    3. For each segment whose speaker is NOT in top-N, relabel it to the
       top-N speaker that is temporally closest (mid-point distance to the
       nearest top-N segment).

    Cheaper than re-running pyannote with ``min=max=hint`` (which doubles
    GPU time and VRAM peak); the approximation is acceptable when the hint
    is treated as advisory rather than ground truth (most Sandinas meetings
    where this hint is supplied).
    """
    if max_speakers <= 0:
        return segments

    durations: dict[str, float] = defaultdict(float)
    for start, end, speaker in segments:
        durations[speaker] += end - start

    if len(durations) <= max_speakers:
        return segments

    top_n = {
        spk
        for spk, _ in sorted(
            durations.items(), key=lambda kv: kv[1], reverse=True
        )[:max_speakers]
    }
    top_n_segments = [s for s in segments if s[2] in top_n]

    def _nearest_top_speaker(mid: float) -> str:
        return min(
            top_n_segments,
            key=lambda s: abs(((s[0] + s[1]) / 2.0) - mid),
        )[2]

    return [
        (start, end, speaker)
        if speaker in top_n
        else (start, end, _nearest_top_speaker((start + end) / 2.0))
        for start, end, speaker in segments
    ]


def diarize(
    pipeline_obj: Any,
    wav_path: Path | str,
    *,
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
) -> list[tuple[float, float, str]]:
    """Run pyannote diarization and return ``list[(start, end, speaker)]``.

    Forwards the optional speaker hints to the pyannote pipeline. If
    ``max_speakers`` is set and the pipeline returns more unique speakers,
    the result is capped by relabel (ALT-3 + D-035: cap-by-relabel rather
    than a strict re-run).

    Maps generic pyannote runtime faults to ``PipelineDiarizeError`` so the
    orchestrator can release the GPU lock and the API can return HTTP 500
    ``PIPELINE_DIARIZE_ERROR`` per the spec catalog.
    """
    # Build kwargs conditionally so the indirection (and the underlying
    # pyannote call inside it) never sees ``key=None``. Some pyannote
    # versions interpret that as "set to None" rather than "not specified".
    kwargs: dict[str, Any] = {}
    if num_speakers is not None:
        kwargs["num_speakers"] = num_speakers
    if min_speakers is not None:
        kwargs["min_speakers"] = min_speakers
    if max_speakers is not None:
        kwargs["max_speakers"] = max_speakers

    try:
        segments = _pyannote_run_pipeline(pipeline_obj, str(wav_path), **kwargs)
    except PipelineDiarizeError:
        raise
    except Exception as exc:
        raise PipelineDiarizeError(str(exc)) from exc

    if max_speakers is not None:
        segments = _cap_speakers_by_duration(segments, max_speakers)

    return segments
