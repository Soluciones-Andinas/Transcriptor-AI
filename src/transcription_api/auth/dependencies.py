"""FastAPI dependencies for authentication.

Spec: SPEC-capa2-auth-msentra-v1
- `get_current_user_web`: parses JWT cookie session (B5).
- `get_current_user_mcp`: validates Authorization Bearer + activates ADR-014
  per-user scoping listener (B6).

Both dependencies set `session.info["user_id"]` after a successful lookup,
which arms the per-user scoping listener so subsequent ORM queries on
that session are automatically filtered.
"""
from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..db.models import User
from .session import SessionInvalid, decode_session_token


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
    request's session.
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

    # Bypass scoping for THIS lookup (the listener filters by user_id which we
    # haven't set yet — chicken-and-egg). Real ORM queries downstream get
    # scoped because we set session.info["user_id"] right after.
    db.info["scoping_bypass"] = True
    try:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    finally:
        db.info.pop("scoping_bypass", None)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "AUTH_NOT_AUTHENTICATED", "reason": "user not found"},
        )

    # Arm the scoping listener for the rest of the request's queries.
    db.info["user_id"] = user.id
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

    On success: sets `db.info["user_id"]` to activate ADR-014 per-user scoping
    on every subsequent ORM query in this request's session. This is THE
    enforcement point — without it, a single forgotten `WHERE user_id = X`
    in any tool/resource query becomes a cross-tenant data leak.

    Side effect: bumps `mcp_bearers.last_used_at = clock_timestamp()` so
    the operator can audit bearer activity.
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

    # Arm the ADR-014 listener for the rest of the request's queries.
    db.info["user_id"] = user.id
    # Persist the last_used_at bump that verify_bearer queued.
    await db.commit()
    return user
