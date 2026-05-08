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
# Tool / resource registrations land here as Batches 2+ tasks ship.
# Each submodule decorates against ``mcp_server`` at import time. The
# imports MUST happen before ``streamable_http_app()`` is invoked
# (see comment at the build line below) — late registration has no
# effect on the ASGI app.
from . import resources as _resources  # noqa: F401, E402 — registers resources
from . import tools as _tools  # noqa: F401, E402 — registers tools at import
from .middleware import (
    BearerAuthMiddleware,
    McpAuthError,
    get_current_bearer_id,
    get_current_user_id,
)
from .server import mcp_server
from .session import mcp_request_session

# Build the ASGI sub-app AFTER all decorators have run. Late registration
# (after this line) has no effect — see the docstring in ``server.py``.
mcp_app = mcp_server.streamable_http_app()

# AC-8 / AC-14 — bearer auth enforced on every /mcp request BEFORE the
# SDK's JSON-RPC layer runs. Attaching to the Starlette sub-app post-
# build is supported by Starlette's middleware stack semantics.
mcp_app.add_middleware(BearerAuthMiddleware)

__all__ = [
    "BearerAuthMiddleware",
    "McpAuthError",
    "get_current_bearer_id",
    "get_current_user_id",
    "mcp_app",
    "mcp_request_session",
    "mcp_server",
]
