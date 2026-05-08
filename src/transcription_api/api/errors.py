"""Canonical error-response helper for the REST endpoints.

Spec: SPEC-capa4-mcp-v1
Covers (G10 — review-fixes Tier 2):
- The audio + image upload handlers (and any future REST endpoint)
  emitted error bodies via inline ``_error_resp`` helpers. Centralizing
  the shape here keeps the JSON contract consistent
  (``{"detail": {"error_code", "reason", ...}}``) and makes it harder
  for a new endpoint to drift to a slightly different envelope.
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def error_response(
    status: int,
    code: str,
    reason: str,
    **extra: Any,
) -> JSONResponse:
    """Build a ``{"detail": {"error_code", "reason", ...}}`` JSONResponse.

    Args:
        status: HTTP status (401 / 404 / 413 / 400 / 503 etc).
        code: spec error code (``MCP_BEARER_INVALID``,
            ``UPLOAD_SESSION_NOT_FOUND``, ...).
        reason: human-readable message; mirrored in ``detail.reason``.
        **extra: optional discriminator fields (``max_bytes``,
            ``retry_after``, ``max_mb``, ...). Merged into the
            ``detail`` dict so MCP clients can unwrap structured info
            alongside the code.
    """
    detail: dict[str, Any] = {"error_code": code, "reason": reason}
    detail.update(extra)
    return JSONResponse(status_code=status, content={"detail": detail})
