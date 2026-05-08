"""Tool error helpers — Capa 4 RF-MCP-* error mapping.

Spec: SPEC-capa4-mcp-v1 §4
The MCP SDK transports tool errors as JSON-RPC errors with a numeric
code (``-32602`` invalid params, ``-32603`` internal). The spec error
catalog uses string discriminators (``INVALID_PARAMETER``,
``FILE_TOO_LARGE``, ``TRANSCRIPTION_NOT_FOUND``, ...). We carry the
spec discriminator in ``ErrorData.data['error_code']`` so MCP clients
can dispatch on it; ``code`` mirrors the JSON-RPC convention based on
HTTP status (4xx -> invalid params, 5xx -> internal).

Usage::

    raise_tool_error("INVALID_PARAMETER", "kind must be ...", 400)

The helper raises ``McpError`` directly (no return) so the tool body
control-flow is obvious.
"""
from __future__ import annotations

from typing import Any, NoReturn

from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData


def raise_tool_error(
    code: str,
    reason: str,
    http_status: int,
    **extra: Any,
) -> NoReturn:
    """Raise ``McpError`` carrying the spec discriminator in ``data``.

    Args:
        code: spec error_code (e.g., ``INVALID_PARAMETER``,
            ``FILE_TOO_LARGE``, ``TRANSCRIPTION_NOT_FOUND``).
        reason: human-readable message; mirrored in
            ``ErrorData.message``.
        http_status: REST equivalent (used by Batch 6 deprecation
            shim and informational for clients).
        **extra: optional ``data`` keys (e.g., ``max_mb=500``).
    """
    rpc_code = INVALID_PARAMS if 400 <= http_status < 500 else INTERNAL_ERROR
    payload: dict[str, Any] = {
        "error_code": code,
        "reason": reason,
        "http_status": http_status,
    }
    payload.update(extra)
    raise McpError(ErrorData(code=rpc_code, message=reason, data=payload))
