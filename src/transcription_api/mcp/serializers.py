"""Pure ORM-row to MCP-payload serializers.

Spec: SPEC-capa4-mcp-v1
Covers (G6 — review-fixes Tier 2):
- Cohesion — five tools (``list``, ``get``, ``search``, plus future
  resource handlers) shared three ad-hoc serialization helpers
  (``_serialize_summary`` / ``_serialize_image`` / ``_serialize_full``)
  that lived inside ``tools/transcription.py``. Centralizing them here
  lets the per-verb tool files (``tools/list.py`` etc.) stay focused on
  the verb-specific logic and lets non-tool consumers (resources, REST
  legacy adapters) reach for the same helpers without circular import
  concerns. Pure functions — no DB, no ContextVar reads, no I/O.
"""
from __future__ import annotations

from typing import Any

from ..db.models import Image, Transcription


def serialize_summary(row: Transcription) -> dict[str, Any]:
    """Compact transcription representation for list / search payloads.

    Excludes the heavy ``text_content`` and ``segments`` blobs so a
    100-row page stays under ~10 KB; clients fetch the full payload via
    ``get_transcription`` or the ``transcription://<id>`` resource.
    """
    return {
        "id": str(row.id),
        "audio_hash": row.audio_hash,
        "original_filename": row.original_filename,
        "duration_seconds": float(row.duration_seconds),
        "language": row.language,
        "num_speakers": row.num_speakers,
        "created_at": row.created_at.isoformat(),
    }


def serialize_image(row: Image) -> dict[str, Any]:
    """Image attachment summary embedded in ``serialize_full``.

    Excludes ``file_path`` from the public payload — that is an internal
    blob path on disk, not a download URL. Clients use the
    ``transcription://<tid>/images/<iid>`` MCP resource for byte access.
    """
    return {
        "id": str(row.id),
        "filename": row.filename,
        "caption": row.caption,
        "mime_type": row.mime_type,
        "size_bytes": row.size_bytes,
    }


def unwrap_segments(blob: Any) -> list[dict[str, Any]]:
    """Normalize the ``segments`` JSONB shape to a bare list.

    Capa 3's pipeline persists segments as ``{"segments": [...]}`` so the
    blob round-trips to disk cache + Postgres consistently. This helper
    handles three forms a future consumer may see:

    - ``{"segments": [...]}`` (canonical) -> the inner list
    - ``[...]`` (raw bare list)            -> as-is
    - ``None`` / unexpected                -> ``[]``
    """
    if isinstance(blob, dict) and "segments" in blob:
        inner = blob["segments"]
        if isinstance(inner, list):
            return inner
        return []
    if isinstance(blob, list):
        return blob
    return []


def serialize_full(
    row: Transcription, images: list[Image]
) -> dict[str, Any]:
    """Full transcription payload returned by ``get_transcription``.

    Mirrors the ``TranscriptionResult`` shape from
    ``wiki/05_modelo_datos.md``: everything from ``serialize_summary``
    plus ``text_content``, normalized ``segments``, ``metadata``, and the
    attached ``images`` array.
    """
    return {
        **serialize_summary(row),
        "text_content": row.text_content,
        "segments": unwrap_segments(row.segments),
        "metadata": row.extra_metadata,
        "images": [serialize_image(img) for img in images],
    }
