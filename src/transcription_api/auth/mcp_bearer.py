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

Capa 2 review S-5: the `scoping_bypass` flag flip is now a context manager
so the cleanup is exception-safe and the cross-user intent is explicit at
the call site.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.sql import func

from ..db.models import McpBearer, User
from ..db.scoping import bypass_scoping

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

    Side-effect: queues `last_used_at = clock_timestamp()` on hit. The
    caller controls when to commit; `get_current_user_mcp` does so on a
    best-effort basis (H-6).

    The lookup runs under `bypass_scoping(session)` because the per-user
    scoping listener would otherwise filter `mcp_bearers` by `user_id`,
    which is exactly the value we are trying to discover.
    """
    if not plaintext:
        return None
    token_hash = hash_bearer(plaintext)

    with bypass_scoping(session):
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

        # Queue last_used_at bump; caller commits (H-6 best-effort).
        await session.execute(
            update(McpBearer)
            .where(McpBearer.id == bearer.id)
            .values(last_used_at=func.clock_timestamp())
        )
        return user
