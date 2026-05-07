"""``mcp/serializers.py`` — pure ORM-to-dict helpers.

Spec: SPEC-capa4-mcp-v1
Covers (G8 — review-fixes Tier 2):
- ``unwrap_segments`` normalizes the JSONB shape to a bare list across
  the three forms a consumer can see: canonical wrapper dict, raw
  list, or None / unexpected. Without this guard, a future migration
  that drops the ``{"segments": ...}`` wrapper would silently break
  the ``get_transcription`` payload.
"""
from __future__ import annotations


def test_unwrap_segments_handles_dict_wrapper() -> None:
    from transcription_api.mcp.serializers import unwrap_segments

    assert unwrap_segments({"segments": [{"start": 0}]}) == [{"start": 0}]


def test_unwrap_segments_handles_bare_list() -> None:
    from transcription_api.mcp.serializers import unwrap_segments

    assert unwrap_segments([{"start": 0}]) == [{"start": 0}]


def test_unwrap_segments_handles_none() -> None:
    from transcription_api.mcp.serializers import unwrap_segments

    assert unwrap_segments(None) == []


def test_unwrap_segments_handles_empty_dict() -> None:
    from transcription_api.mcp.serializers import unwrap_segments

    assert unwrap_segments({}) == []


def test_unwrap_segments_handles_dict_with_non_list_segments() -> None:
    """Defensive: ``{"segments": "broken"}`` collapses to ``[]`` rather
    than passing the string through. Guards against a corrupted JSONB row."""
    from transcription_api.mcp.serializers import unwrap_segments

    assert unwrap_segments({"segments": "broken"}) == []


def test_unwrap_segments_handles_unexpected_scalar() -> None:
    from transcription_api.mcp.serializers import unwrap_segments

    assert unwrap_segments(42) == []
    assert unwrap_segments("string") == []
