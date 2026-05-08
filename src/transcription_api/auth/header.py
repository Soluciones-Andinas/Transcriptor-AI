"""``Authorization: Bearer <token>`` header parser — single source of truth.

Spec: SPEC-capa4-mcp-v1
Covers (G7 — review-fixes Tier 2):
- The MCP middleware, the audio upload endpoint, and the image upload
  endpoint each had their own near-identical inline parser for the
  ``Authorization: Bearer <plaintext>`` format. A drift in one site
  (e.g., accepting trailing whitespace differently) would break parity
  with the other two; centralizing the parser eliminates that risk.

The function returns ``None`` on absent / malformed input so callers
can decide which error code to surface (the API endpoints emit
``MCP_BEARER_INVALID`` and the middleware does the same — the helper
itself is shape-agnostic).
"""
from __future__ import annotations


def parse_bearer(authorization: str | None) -> str | None:
    """Return the plaintext bearer token, or ``None`` on absent / malformed.

    Accepted form: ``Bearer <plaintext>`` (case-insensitive on the
    scheme). Surrounding whitespace on the plaintext is trimmed; an
    empty plaintext after the scheme returns ``None`` (the caller
    surfaces that as ``MCP_BEARER_INVALID``).
    """
    if not authorization:
        return None
    prefix, _, token = authorization.partition(" ")
    if prefix.lower() != "bearer":
        return None
    plaintext = token.strip()
    if not plaintext:
        return None
    return plaintext
