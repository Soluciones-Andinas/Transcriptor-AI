"""``start_transcription`` tool — RF-MCP-02.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 3):
- AC-1 — Consumes a pre-uploaded ``upload_id``, dispatches to the
  Capa 3 ``orchestrator.orchestrate(...)``, flips the row to
  ``status='consumed'``, removes the per-upload dir, and returns
  ``{transcription_id, status='completed', cache_hit}``.
- AC-9 — ``GPUBusy`` from the orchestrator (lock not acquired within
  ``LOCK_WAIT_SECONDS``) maps to spec ``LOCK_BUSY`` (503).
- AC-10 — Unknown / expired / not-yet-uploaded ``upload_id`` all
  return ``UPLOAD_SESSION_NOT_FOUND`` (404). Cross-user collapses to
  the same code via the listener fail-closed (no existence leak).
- ``UPLOAD_SESSION_ALREADY_CONSUMED`` (409) — second call.
- ``MODELS_NOT_LOADED`` (503) — short-circuit before invoking
  orchestrate (saves the GPU acquire round-trip on a degraded node).
- ``PIPELINE_TIMEOUT`` (504) — orchestrator's typed timeout passes
  through with ``timeout_seconds`` in error data.

The tool reads ``current_user_id`` from the bearer middleware
ContextVar (Batch 1). It opens its own ``mcp_request_session`` so the
listener AND-injects ``WHERE user_id = current_user.id`` on the
upload-row SELECT — a cross-user ``upload_id`` therefore yields no row
and surfaces as ``UPLOAD_SESSION_NOT_FOUND``.

Lazy import of ``transcription_api.main:app`` is intentional: the
``main`` module imports the ``mcp`` package at startup, so a top-level
import here would create a cycle. The lazy form runs at tool-call time
when ``app`` is fully constructed.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update

from ...config import settings
from ...db.models import Image, Transcription, UploadSession
from ...pipeline.cache import CacheStore
from ...pipeline.orchestrator import GPUBusy, PipelineTimeout, orchestrate
from ..errors import raise_tool_error
from ..middleware import get_current_user_id
from ..server import mcp_server
from ..session import mcp_request_session

logger = logging.getLogger("transcription_api.mcp.tools.transcription")


# ---------------------------------------------------------------------------
# Sort whitelist for list_my_transcriptions (AC-3). Closed enum so an
# unrecognized value 400s before hitting the DB; mapping to ORM column
# expressions keeps the dispatch terse.
# ---------------------------------------------------------------------------
_LIST_SORTS: dict[str, Any] = {
    "created_at_desc": Transcription.created_at.desc(),
    "created_at_asc": Transcription.created_at.asc(),
    "duration_desc": Transcription.duration_seconds.desc(),
}
_LIST_LIMIT_MAX = 100


def _serialize_summary(row: Transcription) -> dict[str, Any]:
    """Compact row representation used by list_my_transcriptions.

    Excludes the heavy ``segments`` / ``text_content`` fields — clients
    use ``get_transcription`` for the full payload. Keeps list responses
    bounded so a 100-row page stays under ~10 KB.
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


def _serialize_image(row: Image) -> dict[str, Any]:
    """Image attachment summary (used inside _serialize_full).

    Excludes ``file_path`` from the public payload (it's an
    implementation-specific blob path on disk, not a download URL —
    clients use the ``transcription://<tid>/images/<iid>`` MCP
    resource for byte access in Batch 5).
    """
    return {
        "id": str(row.id),
        "filename": row.filename,
        "caption": row.caption,
        "mime_type": row.mime_type,
        "size_bytes": row.size_bytes,
    }


def _serialize_full(
    row: Transcription, images: list[Image]
) -> dict[str, Any]:
    """Full transcription payload returned by get_transcription.

    Mirrors the ``TranscriptionResult`` shape from ``05_modelo_datos.md``:
    everything from ``_serialize_summary`` plus ``text_content``,
    ``segments``, ``metadata``, and the ``images`` array.
    """
    segments_blob = row.segments or {}
    segments = (
        segments_blob.get("segments", [])
        if isinstance(segments_blob, dict)
        else []
    )
    return {
        **_serialize_summary(row),
        "text_content": row.text_content,
        "segments": segments,
        "metadata": row.extra_metadata,
        "images": [_serialize_image(img) for img in images],
    }


@mcp_server.tool(name="start_transcription")
async def start_transcription(
    upload_id: str,
    language: str = "es",
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
) -> dict[str, Any]:
    """Run the audio pipeline against a pre-uploaded session.

    Returns ``{transcription_id, status='completed', cache_hit}``.
    Error mapping in module docstring.
    """
    user_id = get_current_user_id()

    try:
        uid = UUID(upload_id)
    except (TypeError, ValueError):
        raise_tool_error(
            "INVALID_PARAMETER",
            f"upload_id is not a valid UUID: {upload_id!r}",
            400,
        )

    # Models gate — short-circuit before opening a DB session if the
    # lifespan never landed both models in 'ready'. Lazy import of
    # main.app avoids the main <-> mcp circular import.
    from ...main import app  # lazy: avoids main <-> mcp cycle

    whisper_status = getattr(app.state, "whisper_status", "loading")
    pyannote_status = getattr(app.state, "pyannote_status", "loading")
    if whisper_status != "ready" or pyannote_status != "ready":
        which = "whisper" if whisper_status != "ready" else "pyannote"
        which_status = whisper_status if which == "whisper" else pyannote_status
        detail = getattr(app.state, f"{which}_detail", None)
        raise_tool_error(
            "MODELS_NOT_LOADED",
            f"{which} model is not ready (status={which_status})",
            503,
            detail=detail,
        )

    whisper_model = app.state.whisper_model
    pyannote_pipeline = app.state.pyannote_pipeline

    async with mcp_request_session(user_id) as db:
        # AC-10 + cross-user — listener AND-injects user_id; foreign rows
        # surface as None and map to UPLOAD_SESSION_NOT_FOUND.
        row = (
            await db.execute(
                select(UploadSession).where(
                    UploadSession.id == uid,
                    UploadSession.kind == "audio",
                )
            )
        ).scalar_one_or_none()

        if row is None:
            raise_tool_error(
                "UPLOAD_SESSION_NOT_FOUND",
                "upload not found",
                404,
            )

        if row.status == "consumed":
            raise_tool_error(
                "UPLOAD_SESSION_ALREADY_CONSUMED",
                "this upload has already been consumed by start_transcription",
                409,
            )

        # Anything that is not 'uploaded' (still 'requested', or expired)
        # collapses to NOT_FOUND per AC-10 — same body as cross-user /
        # unknown id (no existence leak across causes).
        if row.status != "uploaded" or row.expires_at < datetime.now(timezone.utc):
            raise_tool_error(
                "UPLOAD_SESSION_NOT_FOUND",
                "upload not yet received or expired",
                404,
            )

        upload_dir_for_session = settings.uploads_dir / str(uid)
        original = upload_dir_for_session / "original.bin"

        cache_store = CacheStore(base_dir=settings.cache_dir)

        # Capa 3 invariant: orchestrate handles the lock+timeout+pipeline
        # body. We never touch _orchestrator_lock directly. GPUBusy /
        # PipelineTimeout are typed exceptions with structured fields we
        # surface to the MCP client.
        try:
            result = await orchestrate(
                user_id=user_id,
                db=db,
                file_path=original,
                # D-048: upload_sessions has no original_filename column;
                # synthesize from upload id. Wiki should add the column
                # in Capa 5+ if real filenames matter for audit.
                original_filename=f"upload_{uid}.bin",
                original_size_bytes=row.expected_size_bytes,
                whisper_model=whisper_model,
                pyannote_pipeline=pyannote_pipeline,
                cache_store=cache_store,
                upload_dir=upload_dir_for_session,
                language=language,
                num_speakers=num_speakers,
                min_speakers=min_speakers,
                max_speakers=max_speakers,
            )
        except GPUBusy as exc:
            raise_tool_error(
                "LOCK_BUSY",
                f"GPU busy; retry after {exc.retry_after}s",
                503,
                retry_after=exc.retry_after,
            )
        except PipelineTimeout as exc:
            raise_tool_error(
                "PIPELINE_TIMEOUT",
                f"pipeline exceeded {exc.timeout_seconds}s",
                504,
                timeout_seconds=exc.timeout_seconds,
            )

        # Mark the upload consumed AFTER orchestrate succeeds — keeps
        # the row recoverable on failure (a future retry can still
        # find it 'uploaded' and re-run).
        await db.execute(
            UploadSession.__table__.update()
            .where(UploadSession.id == uid)
            .values(
                status="consumed",
                consumed_at=datetime.now(timezone.utc),
            )
        )
        # mcp_request_session ctx mgr commits on exit.

    # Best-effort cleanup of the per-upload dir. orchestrate already
    # removed the normalized WAV (Capa 3 contract); we remove the raw
    # original.bin + its parent dir so DATA_DIR/uploads/ stays small.
    try:
        if upload_dir_for_session.exists():
            shutil.rmtree(upload_dir_for_session)
    except OSError:
        logger.warning(
            "upload_dir_cleanup_failed path=%s error_id=UPLOAD_DIR_LEAK",
            upload_dir_for_session,
            exc_info=True,
        )

    return {
        "transcription_id": str(result["transcription_id"]),
        "status": "completed",
        "cache_hit": result.get("metadata", {}).get("cache_hit", False),
    }


# ---------------------------------------------------------------------------
# list_my_transcriptions — RF-MCP-04
# ---------------------------------------------------------------------------
@mcp_server.tool(name="list_my_transcriptions")
async def list_my_transcriptions(
    limit: int = 20,
    offset: int = 0,
    sort: str = "created_at_desc",
) -> dict[str, Any]:
    """Paginated list of the caller's transcriptions (AC-3).

    Cross-user isolation comes from the ADR-014/015 listener: the
    SELECT body has NO ``WHERE user_id`` clause; the listener
    AND-injects it from ``mcp_request_session(user_id)``.

    Args:
        limit: max items per page; clamped to ``_LIST_LIMIT_MAX``.
        offset: rows to skip (zero-based).
        sort: one of ``created_at_desc`` (default) /
            ``created_at_asc`` / ``duration_desc``. Anything else ->
            INVALID_PARAMETER (400).

    Returns:
        ``{items: [<summary>...], total, limit, offset}``. Soft-deleted
        rows excluded from BOTH ``items`` and ``total``.
    """
    sort_clause = _LIST_SORTS.get(sort)
    if sort_clause is None:
        raise_tool_error(
            "INVALID_PARAMETER",
            f"sort {sort!r} not in {sorted(_LIST_SORTS)}",
            400,
        )

    if limit > _LIST_LIMIT_MAX:
        limit = _LIST_LIMIT_MAX
    if limit < 1:
        limit = 1
    if offset < 0:
        offset = 0

    user_id = get_current_user_id()
    async with mcp_request_session(user_id) as db:
        items = (
            await db.execute(
                select(Transcription)
                .where(Transcription.deleted_at.is_(None))
                .order_by(sort_clause)
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
        total = (
            await db.execute(
                select(func.count(Transcription.id)).where(
                    Transcription.deleted_at.is_(None)
                )
            )
        ).scalar_one()

    return {
        "items": [_serialize_summary(row) for row in items],
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }


# ---------------------------------------------------------------------------
# search_my_transcriptions — RF-MCP-05
# ---------------------------------------------------------------------------
_SEARCH_LIMIT_MAX = 50
_SEARCH_QUERY_MAX = 200
# Spanish text-search config (PG core; no extension needed).
_FTS_CONFIG = "spanish"
# ts_headline options: bold the matched terms, 5-20 words around match.
_TS_HEADLINE_OPTS = (
    "MaxWords=20, MinWords=5, ShortWord=3, HighlightAll=false"
)


@mcp_server.tool(name="search_my_transcriptions")
async def search_my_transcriptions(
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Spanish full-text search across the caller's transcriptions (AC-4).

    Uses Postgres' built-in ``to_tsvector('spanish', ...) @@
    plainto_tsquery('spanish', ...)`` against the
    ``idx_transcriptions_text_fts`` GIN index (Capa 1 migration).
    Results are ranked by ``ts_rank``; ``ts_headline`` produces a
    snippet showing the matched terms.

    Cross-user isolation: the SELECT body is built via the ORM
    ``select(Transcription.id, ...)`` so the ADR-014/015 listener
    AND-injects ``WHERE user_id = current_user.id``. Raw ``text()``
    SQL would bypass the listener — D-049-style risk avoided.
    """
    cleaned = query.strip()
    if not cleaned:
        raise_tool_error(
            "INVALID_PARAMETER", "query must not be empty", 400
        )
    if len(cleaned) > _SEARCH_QUERY_MAX:
        raise_tool_error(
            "INVALID_PARAMETER",
            f"query exceeds {_SEARCH_QUERY_MAX} chars",
            400,
            max_chars=_SEARCH_QUERY_MAX,
        )

    if limit > _SEARCH_LIMIT_MAX:
        limit = _SEARCH_LIMIT_MAX
    if limit < 1:
        limit = 1

    user_id = get_current_user_id()

    ts_query = func.plainto_tsquery(_FTS_CONFIG, cleaned)
    ts_vector = func.to_tsvector(_FTS_CONFIG, Transcription.text_content)
    rank_expr = func.ts_rank(ts_vector, ts_query).label("rank")
    snippet_expr = func.ts_headline(
        _FTS_CONFIG,
        Transcription.text_content,
        ts_query,
        _TS_HEADLINE_OPTS,
    ).label("snippet")

    stmt = (
        select(
            Transcription.id,
            Transcription.original_filename,
            Transcription.duration_seconds,
            rank_expr,
            snippet_expr,
        )
        .where(Transcription.deleted_at.is_(None))
        .where(ts_vector.op("@@")(ts_query))
        .order_by(rank_expr.desc())
        .limit(limit)
    )

    async with mcp_request_session(user_id) as db:
        rows = (await db.execute(stmt)).all()

    return [
        {
            "id": str(r.id),
            "original_filename": r.original_filename,
            "duration_seconds": float(r.duration_seconds),
            "rank": float(r.rank),
            "snippet": r.snippet,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# get_transcription — RF-MCP-06
# ---------------------------------------------------------------------------
@mcp_server.tool(name="get_transcription")
async def get_transcription(transcription_id: str) -> dict[str, Any]:
    """Full transcription dict for a single id (AC-5).

    Cross-user / unknown / soft-deleted all collapse to
    ``TRANSCRIPTION_NOT_FOUND`` (404) with identical body — no
    existence leak across the three causes.
    """
    user_id = get_current_user_id()

    try:
        tid = UUID(transcription_id)
    except (TypeError, ValueError):
        raise_tool_error(
            "INVALID_PARAMETER",
            f"transcription_id is not a valid UUID: {transcription_id!r}",
            400,
        )

    async with mcp_request_session(user_id) as db:
        # Listener AND-injects user_id; cross-user lookups yield None
        # which we collapse to NOT_FOUND. The deleted_at filter does
        # the same for soft-deleted rows of the OWNER (no leak about
        # the row's history).
        row = (
            await db.execute(
                select(Transcription).where(
                    Transcription.id == tid,
                    Transcription.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise_tool_error(
                "TRANSCRIPTION_NOT_FOUND",
                "transcription not found",
                404,
            )

        # Fetch attached images (also user-scoped + soft-delete-filtered).
        # Image carries user_id denormalized so the listener applies.
        images = (
            await db.execute(
                select(Image).where(
                    Image.transcription_id == tid,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalars().all()

    return _serialize_full(row, list(images))


# ---------------------------------------------------------------------------
# delete_transcription — RF-MCP-09
# ---------------------------------------------------------------------------
@mcp_server.tool(name="delete_transcription")
async def delete_transcription(transcription_id: str) -> dict[str, Any]:
    """Soft-delete the caller's transcription + cascade to its images (AC-11).

    Soft-delete via ``deleted_at = now()`` so the row stays around for
    audit / undelete (Capa 5 may add a hard-purge job). Cascade applies
    to any non-deleted attached Image rows: keeps the per-user
    soft-delete contract symmetric (a deleted parent never has visible
    children).

    Idempotent by listener convention: a second call's UPDATE filters
    out the now-soft-deleted row (``WHERE deleted_at IS NULL``); zero
    rowcount -> NOT_FOUND with the same body as cross-user / unknown
    (no existence-leak across the three causes).
    """
    user_id = get_current_user_id()

    try:
        tid = UUID(transcription_id)
    except (TypeError, ValueError):
        raise_tool_error(
            "INVALID_PARAMETER",
            f"transcription_id is not a valid UUID: {transcription_id!r}",
            400,
        )

    async with mcp_request_session(user_id) as db:
        # Listener AND-injects user_id; cross-user UPDATEs filter out
        # the foreign row, the rowcount stays zero, and we return
        # NOT_FOUND below — no existence leak.
        result = await db.execute(
            update(Transcription)
            .where(
                Transcription.id == tid,
                Transcription.deleted_at.is_(None),
            )
            .values(deleted_at=func.now())
        )
        if result.rowcount == 0:
            raise_tool_error(
                "TRANSCRIPTION_NOT_FOUND",
                "transcription not found",
                404,
            )

        # Cascade — keep the per-user soft-delete contract symmetric so
        # `get_transcription` of the deleted parent never returns its
        # (now-orphan) images. Image carries user_id denormalized so
        # the listener applies the same scoping here.
        await db.execute(
            update(Image)
            .where(
                Image.transcription_id == tid,
                Image.deleted_at.is_(None),
            )
            .values(deleted_at=func.now())
        )

    return {"ok": True}
