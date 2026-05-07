"""Backward-compat re-export shim — Capa 4 review-fix G6.

Spec: SPEC-capa4-mcp-v1
Covers (G6 — review-fixes Tier 2 split):
- The MCP tools that used to live in this file are now in dedicated
  per-verb modules (``start.py``, ``list.py``, ``search.py``,
  ``get.py``, ``delete.py``, ``user.py``) so each file stays under
  ~200 LOC and a future change to one verb does not pull cohesion
  hits across the others.

This module re-exports the public functions and a few private helpers
so existing test imports (``from transcription_api.mcp.tools.transcription
import start_transcription``) keep working without a 38-test-file rename.
The ``@mcp_server.tool`` decorators run inside the per-verb modules at
import time — ``tools/__init__.py`` imports those modules directly so
the registration happens before ``streamable_http_app()`` snapshots.
"""
from __future__ import annotations

from .delete import delete_transcription
from .get import get_transcription
from .list import _LIST_LIMIT_MAX, _LIST_SORTS, list_my_transcriptions
from .search import (
    _FTS_REGCONFIG,
    _SEARCH_LIMIT_MAX,
    _SEARCH_QUERY_MAX,
    _TS_HEADLINE_OPTS,
    _build_fts_predicate,
    _to_tsquery,
    _to_tsvector,
    search_my_transcriptions,
)
from .start import (
    _cleanup_upload_dir,
    _consume_upload,
    _load_upload_row,
    start_transcription,
)
from .user import get_user_info

__all__ = [
    "delete_transcription",
    "get_transcription",
    "get_user_info",
    "list_my_transcriptions",
    "search_my_transcriptions",
    "start_transcription",
    "_LIST_LIMIT_MAX",
    "_LIST_SORTS",
    "_FTS_REGCONFIG",
    "_SEARCH_LIMIT_MAX",
    "_SEARCH_QUERY_MAX",
    "_TS_HEADLINE_OPTS",
    "_build_fts_predicate",
    "_cleanup_upload_dir",
    "_consume_upload",
    "_load_upload_row",
    "_to_tsquery",
    "_to_tsvector",
]
