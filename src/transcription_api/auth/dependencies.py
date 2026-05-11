"""FastAPI dependencies for authentication.

Spec: SPEC-capa2-auth-msentra-v1
- `get_current_user_web`: parses JWT cookie session (B5).
- `get_current_user_mcp`: validates Authorization Bearer + activates ADR-014
  per-user scoping listener (B6).

Both dependencies set `session.info["user_id"]` after a successful lookup,
which arms the per-user scoping listener so subsequent ORM queries on
that session are automatically filtered.

Capa 2 review fixes:
- CR-4: Sessions returned to the pool by SQLAlchemy keep their `.info`
  dict, so we MUST clear `user_id` and `scoping_bypass` post-request to
  avoid a leaked armed-state being reused by a later, unauthenticated
  request that grabs the same session out of the pool.
- S-5: bypass via `bypass_scoping(session)` context manager (instead of
  manual flag flips) — exception-safe and self-documenting.
- H-6: the `last_used_at` bump on bearer hits is best-effort; failing
  to commit it must not 401 a successful auth.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..db.models import User
from ..db.scoping import bypass_scoping
from .session import SessionInvalid, decode_session_token

log = logging.getLogger(__name__)


def _arm_session(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Arm the ADR-014 listener for downstream queries on this request.

    `db.info` survives across pool checkouts; the matching cleanup lives
    in `_disarm_session` which runs after every request via FastAPI's
    dependency cleanup hook (the `try/finally` around the yield in
    `get_session`).
    """
    db.info["user_id"] = user_id


def _disarm_session(db: AsyncSession) -> None:
    """Pop user_id + scoping_bypass so the next pool checkout starts clean."""
    db.info.pop("user_id", None)
    db.info.pop("scoping_bypass", None)


async def get_current_user_web(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the authenticated user from the `session` cookie (web flow).

    Failure modes (all 401 with error_code=AUTH_NOT_AUTHENTICATED):
    - cookie missing
    - cookie signature invalid / expired
    - sub claim missing or malformed UUID
    - user not in DB (user got hard-deleted while session was alive)

    On success: sets `db.info["user_id"]` so the ADR-014 scoping listener
    will inject `WHERE user_id = X` on subsequent ORM queries in this
    request's session. The arming is cleared in `get_session`'s teardown
    (CR-4) so a pooled session reused by a later unauthenticated request
    does not inherit it.
    """
    cookie = request.cookies.get("session")
    if not cookie:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "AUTH_NOT_AUTHENTICATED", "reason": "session cookie missing"},
        )
    try:
        claims = decode_session_token(cookie)
    except SessionInvalid as exc:
        # D-059 / wiki/05 §7: contractual log when a previously-valid cookie
        # fails to decode. ``reason`` discriminates invalid signature vs
        # expired exp claim so operators can spot misconfigured tokens.
        log.info(
            "auth_session_expired reason=%s", exc.__class__.__name__
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "AUTH_NOT_AUTHENTICATED", "reason": "session token invalid or expired"},
        ) from exc

    sub = claims.get("sub")
    try:
        user_id = uuid.UUID(sub)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "AUTH_NOT_AUTHENTICATED", "reason": "session token has invalid sub"},
        ) from exc

    # The user lookup is cross-user by definition (we don't know the user
    # yet). Bypass the listener for this single SELECT only.
    with bypass_scoping(db):
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "AUTH_NOT_AUTHENTICATED", "reason": "user not found"},
        )

    _arm_session(db, user.id)
    return user


async def get_current_user_mcp(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the authenticated user from the `Authorization: Bearer ...` header.

    Used by all Capa 6 MCP routes (tools/resources). Failure modes (401
    AUTH_NOT_AUTHENTICATED):
    - Header missing.
    - Header not in `Bearer <plaintext>` form.
    - Plaintext empty.
    - Plaintext hash not found in mcp_bearers (no row with matching token_hash).
    - Bearer is revoked (revoked_at NOT NULL).

    On success: arms the ADR-014 listener for every subsequent ORM query
    in this request's session. This is THE enforcement point — without it,
    a single forgotten `WHERE user_id = X` in any tool/resource query
    becomes a cross-tenant data leak.

    Side effect: bumps `mcp_bearers.last_used_at = clock_timestamp()` so
    the operator can audit bearer activity. The commit of the bump is
    best-effort (H-6): a transient DB hiccup on this commit must NOT
    flip a valid bearer to 401.
    """
    from .mcp_bearer import verify_bearer

    auth_header = request.headers.get("authorization")
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "AUTH_NOT_AUTHENTICATED", "reason": "missing Authorization header"},
        )

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "AUTH_NOT_AUTHENTICATED", "reason": "Authorization header must be `Bearer <token>`"},
        )

    plaintext = parts[1].strip()
    if not plaintext:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "AUTH_NOT_AUTHENTICATED", "reason": "empty bearer token"},
        )

    user = await verify_bearer(db, plaintext)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "AUTH_NOT_AUTHENTICATED", "reason": "bearer not found or revoked"},
        )

    _arm_session(db, user.id)
    # Best-effort: persist the last_used_at bump that verify_bearer queued.
    # A transient DB error here must not 401 a valid bearer (H-6).
    try:
        await db.commit()
    except Exception:  # noqa: BLE001 — commit failure mode is intentional
        log.warning("failed to persist last_used_at bump for bearer", exc_info=True)
        await db.rollback()
    return user
