"""``list_my_transcriptions`` tool — RF-MCP-04.

Spec: SPEC-capa4-mcp-v1
Covers (G6 — review-fixes Tier 2 split):
- AC-3 — paginated list of caller's transcriptions, listener-scoped
  (no explicit ``WHERE user_id``); soft-deleted rows excluded from
  both items and total. Sort whitelist + clamp policy live here.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from ...db.models import Transcription
from .._clamp import clamp_or_raise
from ..errors import raise_tool_error
from ..middleware import get_current_user_id
from ..serializers import serialize_summary
from ..server import mcp_server
from ..session import mcp_request_session

# Closed enum so an unrecognized sort 400s before hitting the DB; the
# mapping to ORM column expressions keeps the dispatch terse.
_LIST_SORTS: dict[str, Any] = {
    "created_at_desc": Transcription.created_at.desc(),
    "created_at_asc": Transcription.created_at.asc(),
    "duration_desc": Transcription.duration_seconds.desc(),
}
_LIST_LIMIT_MAX = 100


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
        limit: max items per page; clamped via the ``clamp_or_raise``
            grace-window policy (G5 review-fix).
        offset: rows to skip (zero-based); negative values raise.
        sort: one of ``created_at_desc`` / ``created_at_asc`` /
            ``duration_desc``. Anything else -> INVALID_PARAMETER.

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

    limit = clamp_or_raise(limit, lo=1, hi=_LIST_LIMIT_MAX, name="limit")
    if offset < 0:
        raise_tool_error(
            "INVALID_PARAMETER", "offset must be >= 0", 400, min=0
        )

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
        "items": [serialize_summary(row) for row in items],
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }
