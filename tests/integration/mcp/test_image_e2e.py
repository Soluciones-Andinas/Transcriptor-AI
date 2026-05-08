"""End-to-end image flow — AC-7 closure.

Spec: SPEC-capa4-mcp-v1
Covers (G1 — review-fixes Tier 1):
- AC-7 — ``request_upload_url(kind='image', ...)`` -> POST /api/upload-image
  -> ``transcription://<tid>/images/<iid>`` returns the same bytes that
  were uploaded. Wires the three layers together so a regression in any
  segment (URL build, bearer hash compare, blob path layout, resource
  read) breaks this single test rather than hiding behind unit-level
  green.

The codebase pattern for "MCP E2E" is direct invocation: the tool
function is called with ContextVars armed (mirroring what the MCP
middleware does at runtime) and the HTTP step uses ``ASGITransport`` so
no real socket binds. Resource handlers are likewise invoked as
ordinary coroutines. This keeps the test deterministic and avoids
spinning a JSON-RPC transport just to assert the cross-layer contract.
"""
from __future__ import annotations

import io
import secrets
from urllib.parse import urlparse

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from tests.factories import make_bearer, make_transcription, make_user
from transcription_api.auth.mcp_bearer import generate_bearer

pytestmark = pytest.mark.requires_docker


# Same 1×1 transparent PNG used in tests/integration/api/test_upload_image.py.
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _arm_context(user_id, bearer_id):
    """Arm the MCP middleware ContextVars for direct tool invocation.
    Returns a callable that resets both back to the prior tokens."""
    from transcription_api.mcp.middleware import (
        _current_bearer_id,
        _current_user_id,
    )

    user_token = _current_user_id.set(user_id)
    bearer_token = _current_bearer_id.set(bearer_id)

    def _reset():
        _current_user_id.reset(user_token)
        _current_bearer_id.reset(bearer_token)

    return _reset


async def test_image_e2e_full_chain(monkeypatch, tmp_path, session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-7 — Given a user with a transcription owned by them,
    When ``request_upload_url(kind='image', ...)`` returns an URL+bearer
    and the caller POSTs the PNG bytes to that URL with the bearer, Then
    the resource ``transcription://<tid>/images/<iid>`` returns the exact
    same bytes off disk (same blob layout the resource handler expects).
    """
    monkeypatch.setattr("transcription_api.config.settings.data_dir", tmp_path)

    # Seed: user A + active bearer + a transcription owned by A.
    user = await make_user(session, email=f"u-{secrets.token_hex(4)}@x")
    _, main_hash = generate_bearer()
    bearer = await make_bearer(session, user_id=user.id, token_hash=main_hash)
    tx = await make_transcription(session, user_id=user.id)
    await session.commit()

    # 1. Tool: get the upload URL + ephemeral bearer.
    reset = _arm_context(user.id, bearer.id)
    try:
        from transcription_api.mcp.tools.upload import request_upload_url

        tool_resp = await request_upload_url(
            kind="image",
            file_size_bytes=len(PNG_1X1),
            mime_type="image/png",
            transcription_id=str(tx.id),
        )
    finally:
        reset()
    upload_url = tool_resp["upload_url"]
    ephemeral_bearer = tool_resp["bearer"]

    # 2. POST: send the bytes to the issued URL via ASGITransport.
    parsed = urlparse(upload_url)
    relative = f"{parsed.path}?{parsed.query}"
    from transcription_api.main import app

    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as http:
            post_resp = await http.post(
                relative,
                files={"file": ("img.png", io.BytesIO(PNG_1X1), "image/png")},
                headers={"authorization": f"Bearer {ephemeral_bearer}"},
            )
    assert post_resp.status_code == 200, post_resp.text
    image_id = post_resp.json()["image_id"]

    # 3. Resource: pull the bytes back via the MCP resource handler.
    reset = _arm_context(user.id, bearer.id)
    try:
        from transcription_api.mcp.resources import image_resource

        bytes_back = await image_resource(
            transcription_id=str(tx.id),
            image_id=image_id,
        )
    finally:
        reset()

    assert bytes_back == PNG_1X1
