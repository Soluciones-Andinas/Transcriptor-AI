"""Integration tests for GET /auth/me + POST /auth/logout.

Spec: SPEC-capa2-auth-msentra-v1
Plan: docs/sesiones/2026-05-05-capa2-auth-plan.md (T13, T14, T15, T17)
Covers:
- AC-13 (T13): /auth/me with flash → returns plaintext + clears flash cookie.
- AC-14 (T14): /auth/me without flash → returns id only (plaintext null).
- AC-15 (T15): /auth/me unauthenticated → 401 AUTH_NOT_AUTHENTICATED.
- AC-20 (T17): /auth/logout → clears session cookie + 302 to /login.
- ALT-1 (T17): /auth/logout does NOT revoke the active MCP bearer.
"""
from __future__ import annotations

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

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


async def _login_as(session, *, email: str = "alice@sandinas.test"):
    """Seed user + active bearer; return (user, bearer, session_cookie_value)."""
    from tests.factories import make_bearer, make_user
    from transcription_api.auth.session import create_session_token

    user = await make_user(session, email=email)
    bearer = await make_bearer(session, user_id=user.id, name="initial")
    await session.commit()
    token = create_session_token(
        user_id=user.id, ms_oid=user.microsoft_oid, email=user.email,
    )
    return user, bearer, token


# ---------------------------------------------------------------------------
# AC-13 / T13 — /auth/me with flash
# ---------------------------------------------------------------------------
async def test_me_with_flash_returns_plaintext_and_clears(client, session):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-13 — flash cookie present → response includes plaintext;
    Set-Cookie clears the flash on the same response.

    M-10: structured Set-Cookie assertions instead of substring matches.
    H-8: deletion attributes (path/secure/httponly/samesite) must mirror
    the original set so the browser actually drops the cookie.
    """
    from tests.integration.auth._cookies import find_set_cookie, is_cookie_deletion

    user, bearer, session_token = await _login_as(session)

    flash_plaintext = "FLASH_BEARER_PLAINTEXT_xyz"
    r = await client.get(
        "/auth/me",
        cookies={"session": session_token, "mcp_bearer_flash": flash_plaintext},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["email"] == user.email
    assert body["bearer"]["id"] == str(bearer.id)
    assert body["bearer"]["plaintext"] == flash_plaintext

    flash_delete = find_set_cookie(r.headers, "mcp_bearer_flash")
    assert is_cookie_deletion(flash_delete), flash_delete
    assert flash_delete.get("path") == "/", flash_delete
    assert flash_delete.get("secure") is True, flash_delete
    assert flash_delete.get("httponly") is True, flash_delete
    assert str(flash_delete.get("samesite", "")).lower() == "strict", flash_delete


# ---------------------------------------------------------------------------
# AC-14 / T14 — /auth/me without flash
# ---------------------------------------------------------------------------
async def test_me_without_flash_returns_id_only(client, session):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-14 — flash cookie absent → bearer.plaintext is None,
    bearer.id is the active bearer's id.
    """
    user, bearer, session_token = await _login_as(session, email="bob@sandinas.test")

    r = await client.get("/auth/me", cookies={"session": session_token})

    assert r.status_code == 200
    body = r.json()
    assert body["user"]["id"] == str(user.id)
    assert body["bearer"]["id"] == str(bearer.id)
    assert body["bearer"]["plaintext"] is None


# ---------------------------------------------------------------------------
# AC-15 / T15 — /auth/me unauthenticated
# ---------------------------------------------------------------------------
async def test_me_unauthenticated_returns_401(client, session):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-15 — sin cookie session → 401 con error_code AUTH_NOT_AUTHENTICATED.
    """
    r = await client.get("/auth/me")
    assert r.status_code == 401
    body = r.json()
    # FastAPI wraps HTTPException.detail under "detail".
    assert body["detail"]["error_code"] == "AUTH_NOT_AUTHENTICATED"


async def test_me_with_invalid_session_returns_401(client, session):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-15 (variant) — cookie session con firma invalida → 401.
    """
    r = await client.get("/auth/me", cookies={"session": "tampered.cookie.value"})
    assert r.status_code == 401
    assert r.json()["detail"]["error_code"] == "AUTH_NOT_AUTHENTICATED"


# ---------------------------------------------------------------------------
# AC-20 / T17 — /auth/logout
# ---------------------------------------------------------------------------
async def test_logout_clears_session_cookie(client, session):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-20 — POST /auth/logout → 302 a /login + Set-Cookie session
    con Max-Age=0 (o expires en el pasado).

    M-9: la versión anterior afirmaba "subsiguiente /auth/me devuelve 401"
    sin re-mandar la cookie de sesión, lo cual era un FALSE POSITIVE — la
    request siguiente nunca llevaba cookie aunque la deleción no hubiese
    funcionado. Ahora afirmamos sobre la estructura del header de deleción
    (M-9, M-10) y verificamos que coincide con los atributos del set
    original (H-8: path/secure/httponly/samesite).
    """
    from tests.integration.auth._cookies import find_set_cookie, is_cookie_deletion

    _, _, session_token = await _login_as(session, email="carol@sandinas.test")

    r = await client.post("/auth/logout", cookies={"session": session_token})
    assert r.status_code == 302
    assert r.headers["location"] == "/login"

    deletion = find_set_cookie(r.headers, "session")
    assert is_cookie_deletion(deletion), deletion
    assert deletion.get("path") == "/", deletion
    assert deletion.get("secure") is True, deletion
    assert deletion.get("httponly") is True, deletion
    assert str(deletion.get("samesite", "")).lower() == "strict", deletion


# ---------------------------------------------------------------------------
# ALT-1 / T17 — logout NO revoca el MCP bearer
# ---------------------------------------------------------------------------
async def test_logout_does_not_revoke_mcp_bearer(client, session):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: ALT-1 — logout solo borra cookie web; el bearer MCP queda
    activo (revoked_at IS NULL) porque puede estar en uso desde Claude
    Desktop fuera del browser.
    """
    from transcription_api.db.models import McpBearer

    user, bearer, session_token = await _login_as(session, email="dave@sandinas.test")

    r = await client.post("/auth/logout", cookies={"session": session_token})
    assert r.status_code == 302

    # Refresca el bearer desde la DB y confirma que sigue activo.
    await session.refresh(bearer)
    assert bearer.revoked_at is None, (
        "logout debe NO revocar el MCP bearer (ALT-1). El user lo puede "
        "estar usando desde Claude Desktop sin browser."
    )

    # Verificación cruzada: query directa por user_id, debe haber 1 bearer activo.
    bearers_active = (
        await session.execute(
            select(McpBearer)
            .where(McpBearer.user_id == user.id)
            .where(McpBearer.revoked_at.is_(None))
        )
    ).scalars().all()
    assert len(bearers_active) == 1
