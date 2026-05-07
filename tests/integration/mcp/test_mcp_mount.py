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
