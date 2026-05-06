"""Word-to-speaker assignment by mid-point lookup.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-1 — Joins WhisperX timing (per-word ``start``/``end``) with pyannote
  diarization (``[(start, end, speaker)]`` tuples) into the final
  ``segments`` array of the API response. Each word gets a ``speaker``
  field; each segment gets a segment-level ``speaker`` equal to the
  majority across its words (ties broken deterministically by the
  earliest-by-start word).
- ALT-2 (silent audio variant) — Empty transcript returns an empty list;
  empty diarization returns the transcript with sentinel ``"UNKNOWN"``
  speakers so every word/segment always carries the field (downstream
  schema invariant).

Algorithm:
1. For each word in each segment, compute ``mid = (start + end) / 2``.
2. Find the diarization segment whose interval contains ``mid``.
3. If no such segment exists (word lands in a silence gap), assign to
   the diarization segment whose ``[start, end]`` is closest to ``mid``
   (closest endpoint distance).
4. Build the segment-level ``speaker`` by majority vote across the
   segment's word-level speakers (ties broken by first-by-start word).

The implementation is pure-Python with no external dependencies; tests
run on any CPU machine without torch / whisperx / pyannote.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

UNKNOWN_SPEAKER = "UNKNOWN"


def _speaker_for_midpoint(
    midpoint: float,
    diarization: list[tuple[float, float, str]],
) -> str:
    """Find the speaker label for a given word mid-point.

    Returns the speaker of the diarization interval that contains the
    mid-point. If the mid-point falls in a gap, returns the speaker of
    the temporally-closest interval (closest endpoint distance, not
    centroid; we prefer "the speaker who just stopped talking" over
    "the speaker whose centroid happens to be near").
    """
    for start, end, speaker in diarization:
        if start <= midpoint <= end:
            return speaker

    # Fallback: nearest interval by endpoint distance.
    def _distance(seg: tuple[float, float, str]) -> float:
        start, end, _ = seg
        if midpoint < start:
            return start - midpoint
        if midpoint > end:
            return midpoint - end
        return 0.0

    nearest = min(diarization, key=_distance)
    return nearest[2]


def _segment_majority_speaker(
    words_with_speakers: list[dict[str, Any]],
) -> str:
    """Return the most common speaker across ``words_with_speakers``.

    Ties broken by the speaker of the first word (earliest start). The
    word list arrives sorted by start (WhisperX guarantees this), so
    iterating in order and using ``Counter.most_common`` resolves ties
    by first-seen, which matches "earliest start wins".
    """
    counts: Counter[str] = Counter()
    for word in words_with_speakers:
        counts[word["speaker"]] += 1
    # most_common returns highest count first; Counter preserves insertion
    # order on ties — and we inserted in start order — so the tie breaker
    # is naturally "first by start".
    return counts.most_common(1)[0][0]


def assign_speakers_to_words(
    transcript_segments: list[dict[str, Any]],
    diarization_segments: list[tuple[float, float, str]],
) -> list[dict[str, Any]]:
    """Merge word-level transcript with segment-level diarization.

    Returns a NEW list of segment dicts; the input is not mutated. Each
    output segment has a ``speaker`` key (segment-level majority) and
    each word inside has its own ``speaker`` key.

    Edge cases (ALT-2 from spec):
    - Empty transcript -> empty list.
    - Empty diarization -> every word gets ``UNKNOWN_SPEAKER`` so the
      output schema stays invariant.
    """
    if not transcript_segments:
        return []

    out: list[dict[str, Any]] = []
    for segment in transcript_segments:
        new_words: list[dict[str, Any]] = []
        for word in segment["words"]:
            mid = (word["start"] + word["end"]) / 2.0
            speaker = (
                _speaker_for_midpoint(mid, diarization_segments)
                if diarization_segments
                else UNKNOWN_SPEAKER
            )
            new_words.append({**word, "speaker": speaker})

        segment_speaker = (
            _segment_majority_speaker(new_words)
            if new_words
            else UNKNOWN_SPEAKER
        )
        out.append({**segment, "words": new_words, "speaker": segment_speaker})

    return out
