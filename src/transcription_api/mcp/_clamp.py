"""Param clamp policy shared by paginated MCP tools.

Spec: SPEC-capa4-mcp-v1
Covers (G5 — review-fixes Tier 1):
- spec §4 INVALID_PARAMETER — raise on clearly-bug input (negative or
  zero limit, absurd over-cap), keep a *grace window* of silent
  clamping for slightly-too-high values so cooperative-but-loose
  clients still pass without polluting the error surface.

Policy summary, with ``hi`` the per-tool max (``_LIST_LIMIT_MAX=100``,
``_SEARCH_LIMIT_MAX=50``):

    value < lo                -> raise INVALID_PARAMETER
    lo <= value <= hi         -> return value
    hi  < value <= hi*2       -> return hi   (silent clamp, grace window)
    value > hi*2              -> raise INVALID_PARAMETER

The grace window absorbs typos and "I want a few extra" rounding without
silently degrading a request that asked for 9999. Drops the prior policy
of unconditional silent clamp on every out-of-range value, which made
buggy callers indistinguishable from cooperative ones.
"""
from __future__ import annotations

from .errors import raise_tool_error


def clamp_or_raise(value: int, *, lo: int, hi: int, name: str) -> int:
    """Validate and clamp ``value`` for limit/offset-style integers.

    Args:
        value: incoming integer from a tool's signature default override.
        lo: inclusive lower bound (typically 0 for offsets, 1 for limits).
        hi: inclusive upper bound (per-tool max). The grace window is
            ``hi+1`` through ``hi*2``.
        name: argument name used in error messages.

    Returns:
        ``value`` unchanged if in band, ``hi`` if in the grace window.

    Raises:
        ``McpError`` with ``error_code=INVALID_PARAMETER`` on clearly-bug
        input.
    """
    if value < lo:
        raise_tool_error(
            "INVALID_PARAMETER",
            f"{name} must be >= {lo}",
            400,
            min=lo,
        )
    if value <= hi:
        return value
    if value <= hi * 2:
        return hi
    raise_tool_error(
        "INVALID_PARAMETER",
        f"{name} must be <= {hi}",
        400,
        max_limit=hi,
    )
    return hi  # unreachable; satisfies type checkers (raise_tool_error -> NoReturn in G8.1)
