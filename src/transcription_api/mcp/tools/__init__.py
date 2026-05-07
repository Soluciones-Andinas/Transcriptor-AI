"""MCP tools — Capa 4 RF-MCP-* registrations.

Importing this subpackage triggers the ``@mcp_server.tool`` decorators
in each submodule, which registers the tool against the singleton
``mcp_server`` BEFORE ``streamable_http_app()`` is invoked in
``mcp/__init__.py``. Late registration after the ASGI build does not
take effect (the SDK snapshots tools at build time).

G6 review-fix: tools split per verb (start / list / search / get /
delete / user). The ``transcription`` module is a thin re-export shim
that preserves the prior import surface for tests. ``upload`` retains
``request_upload_url``.
"""
from . import (
    delete,  # noqa: F401 — registers delete_transcription
    get,  # noqa: F401 — registers get_transcription
    list,  # noqa: F401 — registers list_my_transcriptions
    search,  # noqa: F401 — registers search_my_transcriptions
    start,  # noqa: F401 — registers start_transcription
    upload,  # noqa: F401 — registers request_upload_url
    user,  # noqa: F401 — registers get_user_info
)

__all__ = [
    "delete",
    "get",
    "list",
    "search",
    "start",
    "upload",
    "user",
]
