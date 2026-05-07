"""MCP module surface — Capa 4 Batch 1.

Spec: SPEC-capa4-mcp-v1
Covers:
- AC-12 — `from transcription_api import mcp` succeeds; the package
  exposes ``mcp_app`` (the Streamable-HTTP ASGI sub-app) and
  ``mcp_server`` (the FastMCP registry where tools/resources land).
  This is the cheapest possible smoke-test for the wire — if the import
  raises, every other Capa 4 test fails for the same reason.

The test runs purely in-process (no network, no DB, no Docker). It
imports the ``mcp`` SDK transitively through ``transcription_api.mcp``;
the SDK is added to project deps in T1.2 GREEN.
"""
from __future__ import annotations


def test_mcp_module_exports_app_and_server():
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-12 — Given the project is installed, When importing
    ``transcription_api.mcp``, Then the module exposes a callable
    ASGI ``mcp_app`` and a ``mcp_server`` instance for tool registration.
    """
    from transcription_api import mcp as _mcp

    assert hasattr(_mcp, "mcp_app"), "mcp_app must be exported"
    assert callable(_mcp.mcp_app), "mcp_app must be ASGI-callable"
    assert hasattr(_mcp, "mcp_server"), "mcp_server registry must be exported"
