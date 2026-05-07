"""Shared helpers + fixtures for integration tests.

Spec: SPEC-capa4-mcp-v1
Covers (G11 — review-fixes Tier 3):
- The MCP integration suite re-implemented the same three helpers in
  every test file: ``_is_tool_error`` (McpError discriminator probe),
  ``_arm_context`` (ContextVar arm/reset for direct tool invocation),
  and ``_seed_user_with_bearer`` (factory boilerplate). Centralizing
  them here lets new tests (Tier 3 + future capas) skip the copy-paste.

Existing test files still ship their own local versions of these
helpers — by design. Migrating ~8 files at once would carry diff +
regression risk that does not pay back the DRY win, so the policy is
"new tests use the conftest, existing tests migrate when touched for
unrelated reasons". The two surfaces stay in lock step because the
local copies were already identical.
"""
from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest


def is_tool_error(exc: Any, expected_code: str) -> bool:
    """Return True iff ``exc`` is an MCP ``McpError`` carrying ``expected_code``.

    The MCP SDK shapes the error as ``McpError`` with ``.error.data`` =
    ``{"error_code": ..., "reason": ..., "http_status": ...}``. This
    helper centralizes the unwrap pattern so a future SDK change only
    breaks one place.
    """
    from mcp.shared.exceptions import McpError

    if not isinstance(exc, McpError):
        return False
    data = getattr(exc.error, "data", None) if hasattr(exc, "error") else None
    return isinstance(data, dict) and data.get("error_code") == expected_code


@pytest.fixture
def assert_tool_error() -> Callable[[Any, str], None]:
    """Pytest fixture wrapper around ``is_tool_error`` for ergonomic asserts.

    Usage::

        with pytest.raises(McpError) as exc:
            await tool(...)
        assert_tool_error(exc.value, "TRANSCRIPTION_NOT_FOUND")
    """

    def _impl(exc: Any, expected_code: str) -> None:
        assert is_tool_error(exc, expected_code), (
            f"expected McpError with error_code={expected_code!r}; "
            f"got {exc!r}"
        )

    return _impl


def arm_context(user_id: UUID, bearer_id: UUID) -> Callable[[], None]:
    """Arm the MCP middleware ContextVars for direct tool invocation.

    Returns a callable that resets both ContextVars to their prior
    tokens. Call sites typically wrap the tool invocation in
    try/finally with the reset to keep tests independent.
    """
    from transcription_api.mcp.middleware import (
        _current_bearer_id,
        _current_user_id,
    )

    user_token = _current_user_id.set(user_id)
    bearer_token = _current_bearer_id.set(bearer_id)

    def _reset() -> None:
        _current_user_id.reset(user_token)
        _current_bearer_id.reset(bearer_token)

    return _reset


async def seed_user_with_bearer(
    session: Any,
    *,
    email_suffix: str,
) -> tuple[Any, Any]:
    """Seed a User + active McpBearer; commit; return ``(user, bearer)``.

    Mirrors the inline helpers in ``test_list_my_transcriptions.py`` etc.
    Centralized so a schema change to either model only needs one
    factory site update.
    """
    from tests.factories import make_bearer, make_user
    from transcription_api.auth.mcp_bearer import generate_bearer

    user = await make_user(session, email=f"u-{email_suffix}-{secrets.token_hex(2)}@x")
    _, token_hash = generate_bearer()
    bearer = await make_bearer(session, user_id=user.id, token_hash=token_hash)
    return user, bearer


__all__ = [
    "arm_context",
    "assert_tool_error",
    "is_tool_error",
    "seed_user_with_bearer",
]


# Suppress F401 on unused fixture re-exports — pytest discovers them by
# name in the conftest, the import itself is the registration.
_: Awaitable[Any] | None = None
