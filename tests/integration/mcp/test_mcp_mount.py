"""MCP sub-app is mounted at /mcp on the FastAPI app.

Spec: SPEC-capa4-mcp-v1
Covers:
- AC-12 — A POST to ``/mcp`` (the canonical Streamable-HTTP transport
  endpoint) MUST NOT return 404 from FastAPI. The exact response code
  is determined by the SDK + bearer middleware (typically 401 without
  a bearer header), but anything other than 404 proves that the
  ``app.mount("/mcp", mcp_app)`` wire is intact.

We avoid asserting specific success codes here because the middleware
contract is owned by Task 1.3/1.4. This test is the wire smoke; the
auth contract has its own tests.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_mcp_endpoint_does_not_404():
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-12 — Given the FastAPI app boots with the MCP sub-app
    mounted, When POST /mcp is sent (with a minimal JSON-RPC initialize
    body), Then the status is anything except 404 — proves the mount
    is wired. 401 / 400 / 200 / 406 are all acceptable here; 404 is
    the failure signal.
    """
    from transcription_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        )

    assert resp.status_code != 404, (
        f"MCP sub-app not mounted; got 404 from FastAPI. "
        f"body={resp.text!r}"
    )


# ---------------------------------------------------------------------------
# G11 — AC-12 canonical surface (tools + resource templates)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_tools_returns_seven_canonical_names():
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-12 (G11 review-fix) — the MCP server MUST register
    the seven Capa 4 verbs and no others. A drift here means the
    ``tools/__init__.py`` import chain (G6 split) is missing a module
    or a stale tool registration is being shadowed.
    """
    # Trigger registration via the same import chain main.py uses.
    import transcription_api.mcp  # noqa: F401
    from transcription_api.mcp.server import mcp_server

    tools = await mcp_server.list_tools()
    names = sorted(t.name for t in tools)
    assert names == sorted(
        [
            "request_upload_url",
            "start_transcription",
            "list_my_transcriptions",
            "search_my_transcriptions",
            "get_transcription",
            "delete_transcription",
            "get_user_info",
        ]
    ), names


@pytest.mark.asyncio
async def test_list_resources_returns_two_uri_templates():
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-12 (G11 review-fix) — MCP resources cover the
    transcription dict and the per-image binary resource. A regression
    that drops either template would break ``transcription://{id}``
    or AC-7 image fetch.
    """
    import transcription_api.mcp  # noqa: F401
    from transcription_api.mcp.server import mcp_server

    templates = sorted(t.uriTemplate for t in await mcp_server.list_resource_templates())
    assert "transcription://{transcription_id}" in templates
    assert any("/images/{image_id}" in t for t in templates), templates
