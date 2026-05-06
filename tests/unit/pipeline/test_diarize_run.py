"""pyannote diarization wrapper.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-1 (diarize side) — `diarize(pipeline_obj, wav_path, ...)` is the
  orchestrator-facing wrapper that returns ``list[(start, end, speaker)]``
  tuples for the merge step (Batch 4 task 4.2). It forwards
  ``num_speakers`` / ``min_speakers`` / ``max_speakers`` hints to the
  pyannote pipeline.
- ALT-3 — When pyannote returns more unique speakers than ``max_speakers``,
  the wrapper caps the result by relabeling the smallest-duration speakers
  to the temporally-closest top-N. "Top-N" is selected by total duration
  across all segments — the most-talkative speakers win.
- PIPELINE_DIARIZE_ERROR (error catalog) — pyannote runtime faults
  (CUDA OOM, missing audio file inside the pipeline, etc.) are remapped
  to ``PipelineDiarizeError`` so the orchestrator (Batch 5) can release
  the GPU lock and the API (Batch 6) can return HTTP 500
  ``PIPELINE_DIARIZE_ERROR`` with a stable body shape.

Tests run on CPU dev machines — pyannote.audio is NOT installed in
``.venv``. The wrapper's internal `_pyannote_run_pipeline` indirection
(introduced by T4.1 GREEN, mirroring `_whisperx_load_model` from
Batch 1) is what tests patch. The pipeline object itself is a plain
``MagicMock`` whose patched indirection returns synthetic tuples.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Canonical diarization shape: list of (start_sec, end_sec, speaker_label).
# This is the contract `pipeline.merge.assign_speakers_to_words` consumes.
# ---------------------------------------------------------------------------
_TWO_SPEAKER_RTTM = [
    (0.0, 5.0, "SPEAKER_00"),
    (5.0, 10.0, "SPEAKER_01"),
    (10.0, 13.0, "SPEAKER_00"),
]


# ---------------------------------------------------------------------------
# AC-1 — happy path
# ---------------------------------------------------------------------------
def test_diarize_returns_list_of_speaker_tuples():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given a pyannote pipeline whose internal call returns
    the canonical RTTM-shaped tuples, When `pipeline.diarize.diarize` is
    invoked, Then the same list of ``(start, end, speaker)`` tuples is
    returned unchanged so the merge step can iterate it directly.
    """
    from transcription_api.pipeline.diarize import diarize

    pipeline_obj = MagicMock(name="pyannote_pipeline")
    with patch(
        "transcription_api.pipeline.diarize._pyannote_run_pipeline",
        return_value=_TWO_SPEAKER_RTTM,
    ):
        out = diarize(pipeline_obj, Path("/tmp/x.wav"))

    assert out == _TWO_SPEAKER_RTTM
    assert all(len(t) == 3 for t in out)
    assert all(isinstance(t[2], str) for t in out)


def test_diarize_passes_wav_path_as_string():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given a pathlib.Path, When forwarded to the
    pyannote pipeline indirection, Then it is coerced to ``str``.
    pyannote 3.x accepts both, but the wrapper normalizes to str so
    downstream logs and stack traces stay consistent.
    """
    from transcription_api.pipeline.diarize import diarize

    pipeline_obj = MagicMock(name="pyannote_pipeline")
    with patch(
        "transcription_api.pipeline.diarize._pyannote_run_pipeline",
        return_value=_TWO_SPEAKER_RTTM,
    ) as run:
        diarize(pipeline_obj, Path("/tmp/x.wav"))

    args, _ = run.call_args
    # First positional arg is the pipeline; second is the wav path string.
    assert args[1] == "/tmp/x.wav"
    assert isinstance(args[1], str)


def test_diarize_forwards_num_speakers_hint():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given an explicit ``num_speakers=3``, When the
    wrapper forwards to the pyannote pipeline, Then the value is
    passed as a kwarg so pyannote skips its speaker-count detection.
    """
    from transcription_api.pipeline.diarize import diarize

    pipeline_obj = MagicMock(name="pyannote_pipeline")
    with patch(
        "transcription_api.pipeline.diarize._pyannote_run_pipeline",
        return_value=_TWO_SPEAKER_RTTM,
    ) as run:
        diarize(pipeline_obj, Path("/tmp/x.wav"), num_speakers=3)

    _, kwargs = run.call_args
    assert kwargs.get("num_speakers") == 3


def test_diarize_omits_speaker_hints_when_not_provided():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given no num/min/max_speakers, When forwarding to
    pyannote, Then no ``*_speakers`` kwargs are passed (lets pyannote
    auto-detect). Avoids passing ``None`` which some pyannote versions
    interpret as the literal None instead of "not specified".
    """
    from transcription_api.pipeline.diarize import diarize

    pipeline_obj = MagicMock(name="pyannote_pipeline")
    with patch(
        "transcription_api.pipeline.diarize._pyannote_run_pipeline",
        return_value=_TWO_SPEAKER_RTTM,
    ) as run:
        diarize(pipeline_obj, Path("/tmp/x.wav"))

    _, kwargs = run.call_args
    for key in ("num_speakers", "min_speakers", "max_speakers"):
        assert key not in kwargs or kwargs[key] is not None


def test_diarize_forwards_min_and_max_speakers():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given ``min_speakers=2, max_speakers=4`` (no
    exact num_speakers), When forwarding, Then both kwargs are passed
    so pyannote constrains its search to that range.
    """
    from transcription_api.pipeline.diarize import diarize

    pipeline_obj = MagicMock(name="pyannote_pipeline")
    with patch(
        "transcription_api.pipeline.diarize._pyannote_run_pipeline",
        return_value=_TWO_SPEAKER_RTTM,
    ) as run:
        diarize(pipeline_obj, Path("/tmp/x.wav"), min_speakers=2, max_speakers=4)

    _, kwargs = run.call_args
    assert kwargs.get("min_speakers") == 2
    assert kwargs.get("max_speakers") == 4


# ---------------------------------------------------------------------------
# ALT-3 — max_speakers cap (cap-by-relabel)
# ---------------------------------------------------------------------------
def test_diarize_caps_extra_speakers_to_max_speakers_hint():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: ALT-3 — Given pyannote returns 3 unique speakers but
    the caller hints ``max_speakers=2``, When the wrapper post-processes
    the result, Then the output has ≤ 2 unique speakers. Strategy:
    keep the top-N speakers by total duration (most-talkative win) and
    relabel the rest to the temporally-closest top-N speaker. The
    canonical 13s sample below has SPEAKER_00 (5+3=8s) and SPEAKER_01
    (5s) as the top two, while a synthetic SPEAKER_02 (1s) gets
    relabeled.
    """
    from transcription_api.pipeline.diarize import diarize

    three_speaker_rttm = [
        (0.0, 5.0, "SPEAKER_00"),
        (5.0, 10.0, "SPEAKER_01"),
        (10.0, 11.0, "SPEAKER_02"),
        (11.0, 14.0, "SPEAKER_00"),
    ]
    pipeline_obj = MagicMock(name="pyannote_pipeline")
    with patch(
        "transcription_api.pipeline.diarize._pyannote_run_pipeline",
        return_value=three_speaker_rttm,
    ):
        out = diarize(pipeline_obj, Path("/tmp/x.wav"), max_speakers=2)

    unique = {seg[2] for seg in out}
    assert len(unique) <= 2
    assert "SPEAKER_02" not in unique


def test_diarize_does_not_cap_when_under_max_speakers():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: ALT-3 — Given pyannote returns 2 unique speakers and
    the caller hints ``max_speakers=3``, When the wrapper checks the
    cap, Then the output is unchanged (no false-positive relabeling).
    """
    from transcription_api.pipeline.diarize import diarize

    pipeline_obj = MagicMock(name="pyannote_pipeline")
    with patch(
        "transcription_api.pipeline.diarize._pyannote_run_pipeline",
        return_value=_TWO_SPEAKER_RTTM,
    ):
        out = diarize(pipeline_obj, Path("/tmp/x.wav"), max_speakers=3)

    assert out == _TWO_SPEAKER_RTTM


# ---------------------------------------------------------------------------
# Error catalog — PIPELINE_DIARIZE_ERROR
# ---------------------------------------------------------------------------
def test_diarize_remaps_runtime_error_to_pipeline_diarize_error():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: error catalog ``PIPELINE_DIARIZE_ERROR`` — Given the
    pyannote pipeline raises a generic RuntimeError (CUDA OOM, missing
    file, etc.), When `diarize` is invoked, Then ``PipelineDiarizeError``
    is raised so the orchestrator (Batch 5) can release the lock and
    the API (Batch 6) can return HTTP 500 with a stable error_code.
    """
    from transcription_api.pipeline.diarize import (
        PipelineDiarizeError,
        diarize,
    )

    pipeline_obj = MagicMock(name="pyannote_pipeline")
    with patch(
        "transcription_api.pipeline.diarize._pyannote_run_pipeline",
        side_effect=RuntimeError("pyannote internal failure"),
    ):
        with pytest.raises(PipelineDiarizeError):
            diarize(pipeline_obj, Path("/tmp/x.wav"))


def test_diarize_does_not_remap_pipeline_diarize_error():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: error catalog (defensive) — Given the indirection itself
    raises a ``PipelineDiarizeError`` (e.g. inner code already wrapped
    a sub-cause), When `diarize` is invoked, Then the existing exception
    propagates unchanged (no double-wrap).
    """
    from transcription_api.pipeline.diarize import (
        PipelineDiarizeError,
        diarize,
    )

    pipeline_obj = MagicMock(name="pyannote_pipeline")
    inner = PipelineDiarizeError("inner already-typed")
    with patch(
        "transcription_api.pipeline.diarize._pyannote_run_pipeline",
        side_effect=inner,
    ):
        with pytest.raises(PipelineDiarizeError) as exc:
            diarize(pipeline_obj, Path("/tmp/x.wav"))
    assert exc.value is inner
