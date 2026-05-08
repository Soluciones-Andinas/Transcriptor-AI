"""Privacy-preserving lookup helper for per-user MCP queries.

Spec: SPEC-capa4-mcp-v1
Covers (G4 — review-fixes Tier 1):
- ADR-014/015 + privacy invariant — five MCP call sites previously
  copy-pasted the SELECT-or-404 pattern. Centralizing the lookup
  forces every site through identical semantics: cross-user, unknown
  id, and (when applicable) soft-deleted rows all collapse to the
  SAME error code + message. A future change to one inline site that
  diverges (e.g., differentiating "not your row" from "doesn't
  exist") would silently leak existence; the helper makes the leak
  surface as a behavior change in this single function.

The helper MUST run inside ``mcp_request_session(user_id)`` so the
ADR-015 listener AND-injects the per-user predicate; the helper itself
does NOT add ``WHERE user_id = ...``. That keeps the contract crisp:
the helper handles the existence-collapse, the listener handles the
ownership-collapse.
"""
from __future__ import annotations

from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .errors import raise_tool_error

T = TypeVar("T")


async def lookup_owned_or_404(
    db: AsyncSession,
    model: type[T],
    id_value,
    *,
    error_code: str,
    error_message: str,
    soft_delete: bool = True,
) -> T:
    """Resolve a per-user row or raise the supplied 404 error.

    Args:
        db: armed AsyncSession (must be inside ``mcp_request_session``).
        model: ORM mapped class (``Transcription``, ``Image``, ...).
        id_value: primary key value (typically a UUID instance).
        error_code: spec error code surfaced on miss
            (``TRANSCRIPTION_NOT_FOUND`` / ``IMAGE_NOT_FOUND``).
        error_message: human-readable message; mirrored in the McpError
            ``message`` and ``data.reason``.
        soft_delete: when True (default) and ``model`` exposes a
            ``deleted_at`` column, the SELECT excludes soft-deleted
            rows so a tombstoned id collapses to the same 404 as
            unknown / cross-user.

    Returns:
        The mapped row instance on hit.

    Raises:
        McpError: with ``data.error_code = error_code`` and
            ``http_status = 404`` on any miss cause.
    """
    stmt = select(model).where(model.id == id_value)
    if soft_delete and hasattr(model, "deleted_at"):
        stmt = stmt.where(model.deleted_at.is_(None))
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise_tool_error(error_code, error_message, 404)
    return row  # type: ignore[return-value]
