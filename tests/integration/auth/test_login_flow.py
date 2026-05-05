"""Integration tests for GET /auth/login (RF-AUTH-01).

Spec: SPEC-capa2-auth-msentra-v1
Plan: docs/sesiones/2026-05-05-capa2-auth-plan.md (T5, T6)
Covers: AC-6 (redirect to MS with PKCE), AC-7 (skip to /mcp-setup if authenticated).
"""
from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient


pytestmark = pytest.mark.requires_docker


@pytest.fixture
async def client():
    from transcription_api.main import app
    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as c:
            yield c


async def test_login_redirects_to_ms_with_pkce(client):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-6 — GET /auth/login redirects to MS with all required PKCE params.
    """
    r = await client.get("/auth/login")
    assert r.status_code == 302
    location = r.headers["location"]
    parsed = urlparse(location)
    assert parsed.netloc == "login.microsoftonline.com"
    assert "/oauth2/v2.0/authorize" in parsed.path

    qs = parse_qs(parsed.query)
    assert qs["client_id"][0]
    assert qs["response_type"] == ["code"]
    assert qs["redirect_uri"][0]
    assert qs["scope"][0] == "openid profile email User.Read"
    assert len(qs["state"][0]) >= 32
    assert len(qs["code_challenge"][0]) >= 43
    assert qs["code_challenge_method"] == ["S256"]

    # Cookie state seteada con flags correctas
    set_cookie = r.headers.get("set-cookie", "")
    assert "oauth_state=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie


async def test_login_generates_unique_state_each_call(client):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-6 cov — calls consecutivas producen state distinto.
    """
    states = set()
    for _ in range(5):
        r = await client.get("/auth/login")
        loc = r.headers["location"]
        states.add(parse_qs(urlparse(loc).query)["state"][0])
    assert len(states) == 5


async def test_login_with_session_skips_to_mcp_setup(client):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-7 — user with valid session cookie redirects directly to /mcp-setup.
    """
    from transcription_api.auth.session import create_session_token

    token = create_session_token(
        user_id=uuid.uuid4(),
        ms_oid=uuid.uuid4(),
        email="alice@sandinas.test",
    )
    r = await client.get("/auth/login", cookies={"session": token})
    assert r.status_code == 302
    assert r.headers["location"] == "/mcp-setup"
    # No oauth_state cookie issued when session is already valid
    assert "oauth_state=" not in r.headers.get("set-cookie", "")
