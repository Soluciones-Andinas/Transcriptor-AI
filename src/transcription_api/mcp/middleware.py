"""Bearer auth middleware for MCP requests (RF-MCP-11).

Spec: SPEC-capa4-mcp-v1
Covers (Batch 1):
- AC-8 — Validates ``Authorization: Bearer <plaintext>`` against the
  ``mcp_bearers.token_hash`` column, distinguishing
  ``MCP_BEARER_INVALID`` (missing / malformed / unknown / no row) from
  ``MCP_BEARER_REVOKED`` (row exists with ``revoked_at IS NOT NULL``).
- AC-14 — On successful auth, bumps ``mcp_bearers.last_used_at`` to
  ``clock_timestamp()`` so operators can audit bearer activity.

The middleware is a Starlette ``BaseHTTPMiddleware`` attached to the
ASGI sub-app at mount time (see ``mcp/__init__.py``). It runs BEFORE
the FastMCP transport handler, so unauthenticated requests never
reach the SDK's JSON-RPC layer.

D-046 / D-047 (logged in this batch):
- The plan assumed Capa 2's ``verify_bearer`` raised typed
  ``BearerInvalid`` / ``BearerRevoked`` exceptions. The real
  ``verify_bearer`` returns ``User | None`` (no exceptions, and a
  revoked row returns None like an unknown bearer). To distinguish
  invalid from revoked we run our own query here (read-only, with
  ``bypass_scoping`` for the unknown-user lookup), without modifying
  ``auth/mcp_bearer.py``.
- ``current_user_id`` is exposed via a ``ContextVar`` so tool
  handlers (Batch 2+) can call ``get_current_user_id()`` synchronously
  inside their bodies without threading the value through every
  signature.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.sql import func
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..auth.mcp_bearer import hash_bearer
from ..db.models import McpBearer
from ..db.scoping import bypass_scoping
from ..db.session import async_session_factory

logger = logging.getLogger("transcription_api.mcp.middleware")

# Per-request context — populated by the middleware on successful auth
# and read by tool handlers via ``get_current_user_id()``. The default
# ``None`` value is what surfaces if a handler runs OUTSIDE the
# middleware path (which is a programming error; we raise rather than
# silently treating it as anonymous).
_current_user_id: ContextVar[UUID | None] = ContextVar(
    "_current_user_id", default=None
)
# Capa 4 Batch 2 — also expose the bearer row id so tools that persist
# rows referencing ``mcp_bearers.id`` (notably ``upload_sessions.bearer_id``
# in ``request_upload_url``) can read it from context instead of querying
# the same row a second time. The middleware armed it during _authenticate,
# so the tool body just calls ``get_current_bearer_id()``.
_current_bearer_id: ContextVar[UUID | None] = ContextVar(
    "_current_bearer_id", default=None
)


class McpAuthError(Exception):
    """Raised internally when bearer validation fails.

    Carries the discriminator string (``MCP_BEARER_INVALID`` or
    ``MCP_BEARER_REVOKED``) so the middleware can render the canonical
    401 body shape.
    """

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


def _unauthorized_response(code: str, reason: str) -> JSONResponse:
    """401 with the canonical MCP error-body shape (spec §4)."""
    return JSONResponse(
        status_code=401,
        content={"detail": {"error_code": code, "reason": reason}},
    )


async def _authenticate(plaintext: str) -> tuple[UUID, UUID]:
    """Validate ``plaintext`` against ``mcp_bearers``; return ``(user_id, bearer_id)``.

    Distinguishes invalid-or-unknown from revoked by querying without
    the ``revoked_at IS NULL`` filter that ``verify_bearer`` applies
    (D-046). Inspects ``revoked_at`` after the lookup and raises the
    appropriate ``McpAuthError`` discriminator.

    Side-effect: bumps ``last_used_at`` on the matched row (best-effort,
    per the H-6 pattern from Capa 2 — a transient DB hiccup on the
    bump must NOT 401 a valid request).

    Capa 4 Batch 2: also returns ``bearer_id`` so the dispatch can arm
    the ``_current_bearer_id`` ContextVar — tools that need to persist
    ``mcp_bearers.id`` references read it from context.
    """
    if not plaintext:
        raise McpAuthError("MCP_BEARER_INVALID", "empty bearer token")

    token_hash = hash_bearer(plaintext)

    async with async_session_factory() as session:
        # Cross-user lookup: we do NOT yet know which user this bearer
        # belongs to. bypass_scoping is the documented seam (ADR-014/015
        # listener) for that single SELECT.
        with bypass_scoping(session):
            row = (
                await session.execute(
                    select(McpBearer).where(McpBearer.token_hash == token_hash)
                )
            ).scalar_one_or_none()

            if row is None:
                raise McpAuthError(
                    "MCP_BEARER_INVALID", "bearer not found"
                )
            if row.revoked_at is not None:
                raise McpAuthError(
                    "MCP_BEARER_REVOKED", "bearer was revoked"
                )

            # AC-14 best-effort last_used_at bump.
            try:
                await session.execute(
                    update(McpBearer)
                    .where(McpBearer.id == row.id)
                    .values(last_used_at=func.clock_timestamp())
                )
                await session.commit()
            except Exception:  # noqa: BLE001 — best-effort H-6
                logger.warning(
                    "mcp_last_used_at_bump_failed bearer_id=%s", row.id,
                    exc_info=True,
                )
                await session.rollback()

            return row.user_id, row.id


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that enforces MCP bearer auth on every request.

    Attached to the MCP sub-app at mount time. The dispatch:
    1. Extract Authorization header.
    2. If missing or not ``Bearer <plaintext>``: return 401
       MCP_BEARER_INVALID without consulting the DB.
    3. Otherwise call ``_authenticate(plaintext)``: a successful return
       arms ``_current_user_id`` for the duration of the downstream
       call; an ``McpAuthError`` is rendered as 401 with the
       discriminator code.
    """

    async def dispatch(self, request: Request, call_next):
        auth_header = request.headers.get("authorization")
        if not auth_header:
            return _unauthorized_response(
                "MCP_BEARER_INVALID", "missing Authorization header"
            )

        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return _unauthorized_response(
                "MCP_BEARER_INVALID",
                "Authorization header must be `Bearer <token>`",
            )

        plaintext = parts[1].strip()
        try:
            user_id, bearer_id = await _authenticate(plaintext)
        except McpAuthError as exc:
            return _unauthorized_response(exc.code, exc.reason)

        user_token = _current_user_id.set(user_id)
        bearer_token = _current_bearer_id.set(bearer_id)
        try:
            return await call_next(request)
        finally:
            _current_user_id.reset(user_token)
            _current_bearer_id.reset(bearer_token)


def get_current_user_id() -> UUID:
    """Read the ``user_id`` armed by the middleware.

    Tool handlers call this synchronously inside their bodies. Raises
    ``McpAuthError`` if the ContextVar is unset, which signals a
    programming error (a tool handler running outside the middleware
    path — not a missing-bearer condition, since the middleware would
    have already 401-ed before reaching the handler).
    """
    uid = _current_user_id.get()
    if uid is None:
        raise McpAuthError(
            "MCP_BEARER_INVALID",
            "current_user_id ContextVar unset (middleware not on path?)",
        )
    return uid


def get_current_bearer_id() -> UUID:
    """Read the ``mcp_bearers.id`` armed by the middleware.

    Used by tools that persist FK references to the active bearer
    (e.g., ``upload_sessions.bearer_id`` in ``request_upload_url``).
    Same programming-error contract as ``get_current_user_id``.
    """
    bid = _current_bearer_id.get()
    if bid is None:
        raise McpAuthError(
            "MCP_BEARER_INVALID",
            "current_bearer_id ContextVar unset (middleware not on path?)",
        )
    return bid
