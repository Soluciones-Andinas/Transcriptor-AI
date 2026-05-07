"""MCP tools — Capa 4 RF-MCP-* registrations.

Importing this subpackage triggers the ``@mcp_server.tool`` decorators
in each submodule, which registers the tool against the singleton
``mcp_server`` BEFORE ``streamable_http_app()`` is invoked in
``mcp/__init__.py``. Late registration after the ASGI build does not
take effect (the SDK snapshots tools at build time).

Add new tools here as their submodules ship.
"""
from . import (
    transcription,  # noqa: F401 — registers start_transcription
    upload,  # noqa: F401 — registers request_upload_url
)

__all__ = ["transcription", "upload"]
