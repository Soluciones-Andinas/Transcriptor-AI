"""FastMCP server factory — Capa 4 RF-MCP-00.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 1):
- AC-12 — Single ``FastMCP`` instance shared with the FastAPI app.
  Tools and resources are registered against this instance via
  ``@mcp_server.tool(name=...)`` and ``@mcp_server.resource(...)``
  decorators in later batches; the registration happens at import
  time of submodules that the package's ``__init__`` imports BEFORE
  building the ASGI sub-app (so every decorator runs first).

The instance is module-level (not factory-built) because FastMCP's
``streamable_http_app()`` snapshots the registered tools at build
time — late registration after the ASGI app is constructed has no
effect. Concentrating the singleton here keeps the registration
contract obvious for reviewers.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# DNS rebinding protection is overly conservative for an intranet-only,
# bearer-authenticated server. Defaults assume the FastMCP host is
# 127.0.0.1/localhost and would only allow Host headers in that allowlist —
# which 421s every request from a real client connecting via a private IP
# (e.g. ZeroTier 10.147.17.0/24, Tailscale 100.x, or LAN). Bearer auth +
# the closed network already establish the trust boundary; the Host check
# is redundant defense-in-depth that breaks deployment.
_TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=False,
)

mcp_server: FastMCP = FastMCP(
    name="transcription-api",
    transport_security=_TRANSPORT_SECURITY,
)
