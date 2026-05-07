"""DB session lifecycle for MCP tool/resource handlers — re-export shim.

Spec: SPEC-capa4-mcp-v1
Covers (G9 — review-fixes Tier 2):
- The implementation now lives in ``db/session.py::scoped_session`` so
  the listener-armed session primitive sits next to the engine + factory
  it depends on. This module preserves the legacy import surface
  (``from transcription_api.mcp.session import mcp_request_session``)
  for tools, resources, and tests that imported it pre-G9.
"""
from __future__ import annotations

from ..db.session import scoped_session as mcp_request_session

__all__ = ["mcp_request_session"]
