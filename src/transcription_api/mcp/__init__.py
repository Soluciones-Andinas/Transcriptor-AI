"""MCP server sub-app for Capa 4.

Spec: SPEC-capa4-mcp-v1
Public exports:
- ``mcp_server``: FastMCP instance — used by submodules to decorate
  ``@mcp_server.tool(...)`` and ``@mcp_server.resource(...)``.
- ``mcp_app``: ASGI Starlette app produced by
  ``mcp_server.streamable_http_app()``. The FastAPI app mounts this at
  ``/mcp`` (see ``main.py``). Streamable-HTTP is the canonical MCP
  transport per ADR-013.

Submodule import order matters: every ``@mcp_server.tool`` decorator
must run BEFORE ``streamable_http_app()`` is invoked, otherwise the
ASGI app does not see the tool. We import submodules first, then build
the app — keep new tool modules added to the imports below.
"""
from .server import mcp_server

# Tool / resource registrations land here as Batches 2+ tasks ship.
# Each submodule decorates against ``mcp_server`` at import time.
# Batch 1: no tools yet (the ``_test_ping`` test-only tool ships with
# Task 1.4 alongside the bearer middleware).

# Build the ASGI sub-app AFTER all decorators have run. Late registration
# (after this line) has no effect — see the docstring in ``server.py``.
mcp_app = mcp_server.streamable_http_app()

__all__ = ["mcp_server", "mcp_app"]
