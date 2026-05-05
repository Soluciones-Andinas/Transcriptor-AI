"""MCP bearer token generation + verification.

Spec: SPEC-capa2-auth-msentra-v1
RF-AUTH-04: emit bearer at first login. RF-AUTH-07: regenerate (revoke + new).

Plaintext format: 64 chars URL-safe (`secrets.token_urlsafe(48)` produces ~64).
Storage: only the SHA-256 hex hash lives in `mcp_bearers.token_hash`. Plaintext
is shown ONCE to the user via the `mcp_bearer_flash` cookie at first login or
via `POST /auth/regenerate-mcp-token` response.

`verify_bearer` is the lookup-side helper used by `get_current_user_mcp`
(Batch 6). It SELECTs the bearer by token_hash, ensures `revoked_at IS NULL`,
returns the joined User; updates `last_used_at = clock_timestamp()`.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.sql import func

from ..db.models import McpBearer, User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def generate_bearer() -> tuple[str, str]:
    """Return (plaintext, token_hash). plaintext is shown once; hash is stored."""
    plaintext = secrets.token_urlsafe(48)  # ~64 url-safe chars
    token_hash = hashlib.sha256(plaintext.encode("ascii")).hexdigest()
    return plaintext, token_hash


def hash_bearer(plaintext: str) -> str:
    """SHA-256 hex of the plaintext (used by middleware on incoming requests)."""
    return hashlib.sha256(plaintext.encode("ascii")).hexdigest()


async def verify_bearer(session: AsyncSession, plaintext: str) -> User | None:
    """Look up the active bearer by hash; return User or None.

    Side-effect: bumps `last_used_at = clock_timestamp()` on hit. The bypass
    flag for the per-user scoping listener (ADR-014) is set via
    `db.info["scoping_bypass"]` while doing this lookup, because the listener
    would otherwise filter `mcp_bearers` by `user_id` which is exactly what
    we're trying to discover.
    """
    if not plaintext:
        return None
    token_hash = hash_bearer(plaintext)

    # Bypass scoping for the lookup itself: the listener would filter by
    # session.info["user_id"] which is unset (we're authenticating).
    session.info["scoping_bypass"] = True
    try:
        stmt = (
            select(User, McpBearer)
            .join(McpBearer, McpBearer.user_id == User.id)
            .where(McpBearer.token_hash == token_hash)
            .where(McpBearer.revoked_at.is_(None))
        )
        result = (await session.execute(stmt)).first()
        if result is None:
            return None
        user, bearer = result

        # Bump last_used_at; not awaiting commit (caller controls tx).
        await session.execute(
            update(McpBearer)
            .where(McpBearer.id == bearer.id)
            .values(last_used_at=func.clock_timestamp())
        )
        return user
    finally:
        session.info.pop("scoping_bypass", None)
