"""``search_my_transcriptions`` tool — RF-MCP-05.

Spec: SPEC-capa4-mcp-v1
Covers (G6 — review-fixes Tier 2 split):
- AC-4 — Spanish full-text search over the caller's transcriptions
  using the GIN functional index ``idx_transcriptions_text_fts``.
  ``_FTS_REGCONFIG`` casts the config name as ``::regconfig`` (G3
  review-fix) so the planner picks the index instead of falling back
  to a sequential scan on ``text_content``.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, literal_column, select

from ...config import settings
from ...db.models import Transcription
from .._clamp import clamp_or_raise
from ..errors import raise_tool_error
from ..middleware import get_current_user_id
from ..server import mcp_server
from ..session import mcp_request_session

_SEARCH_LIMIT_MAX = 50
_SEARCH_QUERY_MAX = 200
# G3 review-fix — literal_column emits the regconfig name with the
# explicit ``::regconfig`` cast, which is the form the GIN functional
# index ``idx_transcriptions_text_fts`` was created on. Without the
# cast Postgres' planner treats the first arg as TEXT and falls back
# to a sequential scan on ``transcriptions.text_content``.
_FTS_REGCONFIG = literal_column(f"'{settings.fts_config}'::regconfig")
# ts_headline options: bold the matched terms, 5-20 words around match.
_TS_HEADLINE_OPTS = (
    "MaxWords=20, MinWords=5, ShortWord=3, HighlightAll=false"
)


def _to_tsvector(col):
    """Indexable tsvector expression — pairs with the GIN functional index."""
    return func.to_tsvector(_FTS_REGCONFIG, col)


def _to_tsquery(query: str):
    """plainto_tsquery wrapper that mirrors ``_to_tsvector``'s regconfig."""
    return func.plainto_tsquery(_FTS_REGCONFIG, query)


def _build_fts_predicate(model, query: str):
    """Return the WHERE expression for full-text search against ``model``.

    Pairs ``to_tsvector(<config>::regconfig, model.text_content)`` with
    ``plainto_tsquery(<config>::regconfig, query)`` via the ``@@`` match
    operator. Extracted so unit tests can compile-and-assert on the
    rendered SQL without spinning a Postgres testcontainer.
    """
    return _to_tsvector(model.text_content).op("@@")(_to_tsquery(query))


@mcp_server.tool(name="search_my_transcriptions")
async def search_my_transcriptions(
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Spanish full-text search across the caller's transcriptions (AC-4).

    Uses Postgres' ``to_tsvector('spanish'::regconfig, ...) @@
    plainto_tsquery('spanish'::regconfig, ...)`` against the
    ``idx_transcriptions_text_fts`` GIN index. Results are ranked by
    ``ts_rank``; ``ts_headline`` produces a snippet showing matched
    terms.

    Cross-user isolation: the SELECT body is built via ORM
    ``select(Transcription.id, ...)`` so the ADR-014/015 listener
    AND-injects ``WHERE user_id = current_user.id``. Raw ``text()``
    SQL would bypass the listener.
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

    limit = clamp_or_raise(limit, lo=1, hi=_SEARCH_LIMIT_MAX, name="limit")

    user_id = get_current_user_id()

    ts_query = _to_tsquery(cleaned)
    ts_vector = _to_tsvector(Transcription.text_content)
    rank_expr = func.ts_rank(ts_vector, ts_query).label("rank")
    snippet_expr = func.ts_headline(
        _FTS_REGCONFIG,
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
