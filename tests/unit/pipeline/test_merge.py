"""Word-to-speaker assignment by mid-point lookup.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-1 — `assign_speakers_to_words(transcript_segments, diarization_segments)`
  is the merge step that joins WhisperX timing (per-word start/end) with
  pyannote diarization (per-segment speaker labels) into the final
  ``segments`` array of the API response. Each word gets a ``speaker``
  field; each segment also gets a ``speaker`` field equal to the
  majority-vote speaker across its words (ties broken by first-by-start).
- ALT-2 (silent audio variant) — Empty transcript or empty diarization
  must NOT crash the merge (degenerate but valid input).

Tests use only stdlib + pytest; no torch / whisperx / pyannote needed.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Canonical fixtures matching the spec's example shapes.
# ---------------------------------------------------------------------------
def _make_transcript():
    """Three-word transcript across two segments (matches plan §T4.2)."""
    return [
        {
            "start": 0.0,
            "end": 1.0,
            "text": "Hola",
            "words": [
                {"word": "Hola", "start": 0.0, "end": 1.0},
            ],
        },
        {
            "start": 1.0,
            "end": 2.0,
            "text": "soy yo",
            "words": [
                {"word": "soy", "start": 1.0, "end": 1.5},
                {"word": "yo", "start": 1.5, "end": 2.0},
            ],
        },
    ]


# ---------------------------------------------------------------------------
# AC-1 — happy path mid-point assignment
# ---------------------------------------------------------------------------
def test_merge_assigns_each_word_to_overlapping_speaker():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 (plan §T4.2) — Given 3 words across 2 segments and
    diarization with two non-overlapping speaker spans, When merge runs,
    Then each word gets ``speaker`` equal to the diarization speaker
    whose ``[start, end]`` interval contains its ``(start+end)/2``.
    """
    from transcription_api.pipeline.merge import assign_speakers_to_words

    transcript = _make_transcript()
    diarization = [(0.0, 0.9, "SPEAKER_00"), (1.0, 2.0, "SPEAKER_01")]

    out = assign_speakers_to_words(transcript, diarization)

    assert out[0]["words"][0]["speaker"] == "SPEAKER_00"
    assert out[1]["words"][0]["speaker"] == "SPEAKER_01"
    assert out[1]["words"][1]["speaker"] == "SPEAKER_01"


def test_merge_preserves_word_metadata_unchanged():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given a transcript with ``word``/``start``/``end``
    on each word, When merge runs, Then those existing fields are
    preserved unchanged; only ``speaker`` is added. The merge must
    NOT mutate timing or text (downstream UI relies on them).
    """
    from transcription_api.pipeline.merge import assign_speakers_to_words

    transcript = _make_transcript()
    diarization = [(0.0, 2.0, "SPEAKER_00")]

    out = assign_speakers_to_words(transcript, diarization)

    assert out[0]["words"][0]["word"] == "Hola"
    assert out[0]["words"][0]["start"] == 0.0
    assert out[0]["words"][0]["end"] == 1.0
    assert out[1]["words"][1]["word"] == "yo"
    assert out[1]["words"][1]["start"] == 1.5
    assert out[1]["words"][1]["end"] == 2.0


def test_merge_preserves_segment_metadata_unchanged():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Segment-level ``start``/``end``/``text`` must be
    preserved; only ``speaker`` is added. Merge does not regroup or
    re-time segments — that is WhisperX's responsibility upstream.
    """
    from transcription_api.pipeline.merge import assign_speakers_to_words

    transcript = _make_transcript()
    diarization = [(0.0, 2.0, "SPEAKER_00")]

    out = assign_speakers_to_words(transcript, diarization)

    assert out[0]["start"] == 0.0
    assert out[0]["end"] == 1.0
    assert out[0]["text"] == "Hola"
    assert out[1]["start"] == 1.0
    assert out[1]["end"] == 2.0
    assert out[1]["text"] == "soy yo"


def test_merge_assigns_segment_speaker_by_majority_vote():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — A segment's ``speaker`` field is the majority
    speaker across its words. When two speakers tie, the one whose
    first contributing word starts earlier wins (deterministic).
    """
    from transcription_api.pipeline.merge import assign_speakers_to_words

    transcript = [
        {
            "start": 0.0,
            "end": 3.0,
            "text": "uno dos tres",
            "words": [
                {"word": "uno", "start": 0.0, "end": 1.0},
                {"word": "dos", "start": 1.0, "end": 2.0},
                {"word": "tres", "start": 2.0, "end": 3.0},
            ],
        }
    ]
    # Two words land in SPEAKER_A, one in SPEAKER_B → segment majority A.
    diarization = [(0.0, 2.0, "SPEAKER_A"), (2.0, 3.0, "SPEAKER_B")]

    out = assign_speakers_to_words(transcript, diarization)

    assert out[0]["speaker"] == "SPEAKER_A"
    assert out[0]["words"][0]["speaker"] == "SPEAKER_A"
    assert out[0]["words"][1]["speaker"] == "SPEAKER_A"
    assert out[0]["words"][2]["speaker"] == "SPEAKER_B"


# ---------------------------------------------------------------------------
# Word-in-gap fallback
# ---------------------------------------------------------------------------
def test_merge_assigns_word_in_gap_to_nearest_speaker():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given a word whose mid-point falls in a silence
    gap between two diarization segments, When merge runs, Then the
    word's ``speaker`` is the speaker of the temporally-closest
    diarization segment (smallest distance from the word's mid-point
    to the segment's ``[start, end]`` interval).
    """
    from transcription_api.pipeline.merge import assign_speakers_to_words

    # Word "puente" mid-point at 2.0 falls in a silence gap [1.8, 2.5]
    # between two speakers; closest segment is SPEAKER_LEFT (gap end of
    # 1.8 is at distance 0.2 from 2.0; SPEAKER_RIGHT starts at 2.5 → 0.5).
    transcript = [
        {
            "start": 1.5,
            "end": 2.5,
            "text": "puente",
            "words": [{"word": "puente", "start": 1.5, "end": 2.5}],
        }
    ]
    diarization = [
        (0.0, 1.8, "SPEAKER_LEFT"),
        (2.5, 4.0, "SPEAKER_RIGHT"),
    ]

    out = assign_speakers_to_words(transcript, diarization)

    assert out[0]["words"][0]["speaker"] == "SPEAKER_LEFT"


# ---------------------------------------------------------------------------
# ALT-2 — empty inputs
# ---------------------------------------------------------------------------
def test_merge_returns_empty_list_when_transcript_is_empty():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: ALT-2 — Given empty transcript_segments, When merge runs,
    Then an empty list is returned (degenerate case for silent audio
    where Whisper produced zero segments). Must not crash.
    """
    from transcription_api.pipeline.merge import assign_speakers_to_words

    out = assign_speakers_to_words([], [(0.0, 1.0, "SPEAKER_00")])
    assert out == []


def test_merge_assigns_unknown_speaker_when_diarization_is_empty():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: ALT-2 (defensive) — Given empty diarization (pyannote
    found no speakers, e.g. very short clip), When merge runs, Then
    each word gets a sentinel ``"UNKNOWN"`` speaker so the downstream
    pipeline does not silently drop the word, and the schema stays
    consistent (every word always has a ``speaker`` field).
    """
    from transcription_api.pipeline.merge import assign_speakers_to_words

    transcript = _make_transcript()
    out = assign_speakers_to_words(transcript, [])

    for segment in out:
        assert segment["speaker"] == "UNKNOWN"
        for word in segment["words"]:
            assert word["speaker"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Defensive: input is not mutated
# ---------------------------------------------------------------------------
def test_merge_does_not_mutate_input_transcript():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 (defensive) — Given a transcript dict, When merge
    runs, Then the original transcript object remains unchanged
    (the orchestrator may want to log the raw STT output separately
    from the merged result).
    """
    from transcription_api.pipeline.merge import assign_speakers_to_words

    transcript = _make_transcript()
    diarization = [(0.0, 2.0, "SPEAKER_00")]

    assign_speakers_to_words(transcript, diarization)

    # Original word dicts should not have a 'speaker' key after the call.
    assert "speaker" not in transcript[0]["words"][0]
    assert "speaker" not in transcript[0]
