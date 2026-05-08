"""``get_user_info`` tool — caller-identity surface for Capa 4.

Spec: SPEC-capa4-mcp-v1
Covers (G6 — review-fixes Tier 2 split):
- Surfaces ``user_id`` / ``email`` / ``display_name`` / ``bearer_id``
  for the active call, derived from the bearer middleware's
  ContextVars. The User model is NOT a per-user-scoped model
  (``db.scoping._scoped_models`` excludes it because User IS the per-user
  root entity), so the listener leaves the SELECT alone — no
  ``bypass_scoping`` needed.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ...db.models import User
from ..middleware import get_current_bearer_id, get_current_user_id
from ..server import mcp_server
from ..session import mcp_request_session


@mcp_server.tool(name="get_user_info")
async def get_user_info() -> dict[str, Any]:
    """Return the caller's identity (user + active bearer).

    No input args; the bearer middleware armed both ContextVars
    (``_current_user_id`` and ``_current_bearer_id``) on the request,
    and we serialize them + the User row's display fields.
    """
    user_id = get_current_user_id()
    bearer_id = get_current_bearer_id()

    async with mcp_request_session(user_id) as db:
        user = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one()

    return {
        "user_id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "bearer_id": str(bearer_id),
    }
