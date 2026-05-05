# Plan TDD — Capa 2: Microsoft Entra OAuth + Auth endpoints + MCP bearer middleware

> **For Claude:** REQUIRED SUB-SKILL: Use `sandinas-dev-workflows:executing-plans` to implement this plan.
> **Workflow:** TDD (Red-Green-Refactor) with atomic commits per task.

**Spec source:** `docs/sesiones/2026-05-05-capa2-auth-spec.md` (commit `89f0a61`)
**Spec ID:** `SPEC-capa2-auth-msentra-v1`
**Branch:** `feat/capa2-auth-msentra` (base: `master @ 57bfe81`)
**Goal:** Implementar el flow OAuth 2.0 con PKCE contra Microsoft Entra ID, los endpoints `/auth/*`, y el middleware MCP bearer que activa el listener de per-user scoping de ADR-014.

**Tech stack:** Python 3.11, FastAPI, SQLAlchemy 2.0 (Capa 1), `authlib>=1.3` (OAuth + JWT), `cryptography>=42` (AES-GCM), `itsdangerous>=2.2` (cookie state). Tests: pytest + pytest-asyncio + `respx` (mock HTTP para MS Entra) + testcontainers[postgres] (heredado de Capa 1).

**Test strategy:**
- 20 acceptance criteria → ~22 test functions (algunos parametrize).
- 5 error cases → 4 dedicated tests (AUTH_INVALID_STATE, AUTH_INVALID_OAUTH_CODE via tampered token, AUTH_TENANT_NOT_ALLOWED, AUTH_PROVIDER_UNAVAILABLE — el AUTH_NOT_AUTHENTICATED se cubre en cada endpoint protegido).
- 3 alternative flows: ALT-1 (logout deja bearer activo) → test asertivo; ALT-2/ALT-3 son arquitecturales, sin test directo.
- Total: ~28 tests nuevos. Heredamos los 95 de Capa 1 (deben seguir verde).

---

## Test Mapping

| # | AC / ERR / ALT | Acceptance Criterion | Test Function | File |
|---|---|---|---|---|
| 1 | AC-1 | Auth module importable + dependencies expuestas | `test_auth_module_imports` | `tests/integration/auth/test_module.py` |
| 2 | AC-2 | Crypto round-trip + nonce random | `test_crypto_roundtrip_unique_nonce` | `tests/unit/auth/test_crypto.py` |
| 3 | AC-3 | Crypto tamper detection | `test_crypto_tamper_raises_invalid_tag` | `tests/unit/auth/test_crypto.py` |
| 4 | ERR-2 | OAUTH_TOKEN_ENC_KEY inválida → startup fail | `test_invalid_enc_key_fails_loud` | `tests/unit/auth/test_crypto.py` |
| 5 | AC-4 | State cookie sign/verify + TTL | `test_state_cookie_roundtrip_and_expiry` | `tests/unit/auth/test_state_cookie.py` |
| 6 | AC-5 | Session JWT round-trip + signature/expiry | `test_session_jwt_roundtrip_and_validation` | `tests/unit/auth/test_session_jwt.py` |
| 7 | AC-6 | `GET /auth/login` redirect 302 a MS con state + PKCE | `test_login_redirects_to_ms_with_pkce` | `tests/integration/auth/test_login_flow.py` |
| 8 | AC-6 cov | States distintos en calls consecutivos | `test_login_generates_unique_state_each_call` | `tests/integration/auth/test_login_flow.py` |
| 9 | AC-7 | User logueado → 302 directo a /mcp-setup | `test_login_with_session_skips_to_mcp_setup` | `tests/integration/auth/test_login_flow.py` |
| 10 | AC-8 | Callback primer login → INSERT user/tokens/bearer + flash | `test_callback_first_login_creates_all_rows` | `tests/integration/auth/test_callback_flow.py` |
| 11 | AC-9 | Callback subsiguiente → UPDATE last_login, NO nuevo bearer | `test_callback_subsequent_login_updates_only` | `tests/integration/auth/test_callback_flow.py` |
| 12 | AC-19 | Tokens MS encriptados (BYTEA no contiene plaintext) | `test_oauth_tokens_stored_encrypted_not_plaintext` | `tests/integration/auth/test_callback_flow.py` |
| 13 | AC-10 / ERR | State mismatch → 302 /login?error=AUTH_INVALID_STATE | `test_callback_state_mismatch_redirects_with_error` | `tests/integration/auth/test_callback_flow.py` |
| 14 | AC-11 / ERR | Tenant rechazado → 302 /login?error=AUTH_TENANT_NOT_ALLOWED | `test_callback_foreign_tenant_redirects_with_error` | `tests/integration/auth/test_callback_flow.py` |
| 15 | AC-12 / ERR | MS unavailable → 302 /login?error=AUTH_PROVIDER_UNAVAILABLE | `test_callback_ms_unavailable_redirects_with_error` | `tests/integration/auth/test_callback_flow.py` |
| 16 | AC-13 | `/auth/me` con flash → 200 + bearer.plaintext + flash borrada | `test_me_with_flash_returns_plaintext_and_clears` | `tests/integration/auth/test_me_endpoint.py` |
| 17 | AC-14 | `/auth/me` sin flash → 200 + bearer.plaintext null | `test_me_without_flash_returns_id_only` | `tests/integration/auth/test_me_endpoint.py` |
| 18 | AC-15 | `/auth/me` sin auth → 401 AUTH_NOT_AUTHENTICATED | `test_me_unauthenticated_returns_401` | `tests/integration/auth/test_me_endpoint.py` |
| 19 | AC-16 | `/auth/regenerate-mcp-token` revoca viejo + emite nuevo | `test_regenerate_revokes_old_and_issues_new` | `tests/integration/auth/test_regenerate_endpoint.py` |
| 20 | ERR-4 | Race en doble regenerate → partial UNIQUE catch + retry | `test_regenerate_handles_race_via_partial_unique` | `tests/integration/auth/test_regenerate_endpoint.py` |
| 21 | AC-17 | MCP middleware: 401 sin Bearer / valid → 200 + scoping seteado / revoked → 401 | `test_mcp_bearer_middleware` (parametrize) | `tests/integration/auth/test_mcp_bearer_middleware.py` |
| 22 | AC-18 | Cross-user isolation: bearer A no ve datos de B (activa ADR-014) | `test_mcp_bearer_activates_per_user_scoping` | `tests/integration/auth/test_mcp_bearer_middleware.py` |
| 23 | AC-20 | Logout borra cookie session + /auth/me siguiente → 401 | `test_logout_clears_session_cookie` | `tests/integration/auth/test_me_endpoint.py` |
| 24 | ALT-1 | Logout NO revoca bearer MCP (sigue valid) | `test_logout_does_not_revoke_mcp_bearer` | `tests/integration/auth/test_me_endpoint.py` |

---

## Batch Plan

| Batch | Tasks | ACs | Goal |
|---|---|---|---|
| **B1 — Foundation** | T1, T2, T3, T4 | AC-1, AC-2, AC-3, ERR-2, AC-4, AC-5 | Crypto + state cookie + session JWT helpers (unit-testable, sin DB/HTTP) |
| **B2 — Login flow** | T5, T6 | AC-6, AC-7 | `GET /auth/login` con PKCE + redirect a MS + skip si logged in |
| **B3 — Callback happy paths** | T7, T8, T9 | AC-8, AC-9, AC-19 | Primer login + subsiguiente + tokens encriptados verificados |
| **B4 — Callback errors** | T10, T11, T12 | AC-10, AC-11, AC-12 | State mismatch + tenant rechazado + MS unavailable |
| **B5 — `/auth/me` + regenerate + logout** | T13, T14, T15, T16, T17 | AC-13, AC-14, AC-15, AC-16, AC-20, ERR-4, ALT-1 | Endpoints user-facing post-login |
| **B6 — MCP middleware + cross-user** | T18, T19 | AC-17, AC-18 | Dependency `get_current_user_mcp` + integración con ADR-014 |

Stop and report after each batch. Wait for "continue" before next batch.

---

## Tasks

### Task T1 — Foundation: deps + auth/__init__.py + crypto helpers (AC-1, AC-2, AC-3, ERR-2)

**Source:** SPEC-capa2-auth-msentra-v1, AC-1/AC-2/AC-3 + ERR-2
**Criterion:** módulo importable, encrypt/decrypt round-trip con nonce random, tamper detection, startup fail si OAUTH_TOKEN_ENC_KEY inválida.

**Files:**
- Test: `tests/unit/auth/test_crypto.py::{test_crypto_roundtrip_unique_nonce, test_crypto_tamper_raises_invalid_tag, test_invalid_enc_key_fails_loud}` + `tests/integration/auth/test_module.py::test_auth_module_imports`
- Impl: `src/transcription_api/auth/__init__.py`, `src/transcription_api/auth/crypto.py`, `pyproject.toml` (add `authlib`, `cryptography`, `itsdangerous`, `respx[dev]`)

**RED:**
```python
# tests/unit/auth/test_crypto.py
import pytest
from cryptography.exceptions import InvalidTag

def test_crypto_roundtrip_unique_nonce():
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-2 — encrypt/decrypt round-trip; same plaintext produces
    different ciphertexts (nonce random per call).
    """
    from transcription_api.auth.crypto import encrypt_token, decrypt_token
    plaintext = "ya29.a0AfH6SMBexample-access-token-from-microsoft"
    ct1 = encrypt_token(plaintext)
    ct2 = encrypt_token(plaintext)
    assert isinstance(ct1, bytes) and len(ct1) > 12  # at least nonce + ct
    assert ct1 != ct2  # nonce random ⇒ ciphertext distinto
    assert decrypt_token(ct1) == plaintext
    assert decrypt_token(ct2) == plaintext


def test_crypto_tamper_raises_invalid_tag():
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-3 — modificar 1 byte del ciphertext debe raise InvalidTag.
    """
    from transcription_api.auth.crypto import encrypt_token, decrypt_token
    ct = bytearray(encrypt_token("secret"))
    ct[-1] ^= 0xFF  # flip último byte (parte del tag)
    with pytest.raises(InvalidTag):
        decrypt_token(bytes(ct))


def test_invalid_enc_key_fails_loud(monkeypatch):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Error: ERR-2 — OAUTH_TOKEN_ENC_KEY que no es 32 bytes raise al import,
    no en el primer encrypt en runtime.
    """
    from transcription_api.config import settings
    # Forzar key inválida
    from pydantic import SecretStr
    monkeypatch.setattr(settings, "oauth_token_enc_key", SecretStr("too-short"))
    # El módulo debe rechazar la key cuando se cargue el AESGCM cipher.
    import importlib
    from transcription_api.auth import crypto
    with pytest.raises((ValueError, RuntimeError)):
        importlib.reload(crypto)
```

```python
# tests/integration/auth/test_module.py
def test_auth_module_imports():
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-1 — módulo auth expone router + dependencies.
    """
    from transcription_api.auth import router, get_current_user_web, get_current_user_mcp
    from fastapi import APIRouter
    assert isinstance(router, APIRouter)
    assert callable(get_current_user_web)
    assert callable(get_current_user_mcp)
```

Run: tests fallan (módulo no existe).

**GREEN:**
1. `pyproject.toml`: agregar `authlib>=1.3`, `cryptography>=42`, `itsdangerous>=2.2` a deps; `respx>=0.21` a `[dev]`.
2. `pip install -e ".[dev]"`.
3. `src/transcription_api/auth/__init__.py`:
```python
from .routes import router
from .dependencies import get_current_user_web, get_current_user_mcp

__all__ = ["router", "get_current_user_web", "get_current_user_mcp"]
```
4. `src/transcription_api/auth/crypto.py`:
```python
"""AES-256-GCM encryption for OAuth tokens stored in oauth_tokens.ms_*_encrypted.

Spec: SPEC-capa2-auth-msentra-v1, AC-2 / AC-3 / ERR-2
Closed decision RF-AUTH-02: AES-256-GCM with OAUTH_TOKEN_ENC_KEY.
"""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import settings


def _load_key() -> bytes:
    raw = settings.oauth_token_enc_key.get_secret_value()
    try:
        # Accept urlsafe-b64 of 32 bytes.
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as e:
        raise RuntimeError(f"OAUTH_TOKEN_ENC_KEY is not valid urlsafe-base64: {e}") from e
    if len(key) != 32:
        raise RuntimeError(
            f"OAUTH_TOKEN_ENC_KEY must decode to 32 bytes, got {len(key)} bytes"
        )
    return key


_AESGCM = AESGCM(_load_key())


def encrypt_token(plaintext: str) -> bytes:
    """Encrypt with AES-256-GCM. Output: nonce (12 bytes) || ciphertext || tag."""
    nonce = os.urandom(12)
    ct = _AESGCM.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    return nonce + ct


def decrypt_token(ciphertext: bytes) -> str:
    """Decrypt; raises cryptography.exceptions.InvalidTag on tamper."""
    nonce, ct = ciphertext[:12], ciphertext[12:]
    pt = _AESGCM.decrypt(nonce, ct, associated_data=None)
    return pt.decode("utf-8")
```

**Commits RED → GREEN:**
- `test(auth): SPEC-capa2 AC-1+AC-2+AC-3+ERR-2 — crypto + module imports`
- `feat(auth): SPEC-capa2 AC-1+AC-2+AC-3+ERR-2 — auth.crypto with AES-256-GCM + deps`

---

### Task T2 — State cookie helper (AC-4)

**Source:** SPEC-capa2-auth-msentra-v1, AC-4
**Criterion:** sign/verify cookie temp `oauth_state` con `itsdangerous`, expira tras 5 min.

**Files:**
- Test: `tests/unit/auth/test_state_cookie.py::test_state_cookie_roundtrip_and_expiry`
- Impl: `src/transcription_api/auth/state_cookie.py`

**RED:**
```python
import pytest
import time

from transcription_api.auth.state_cookie import sign_state, verify_state, StateExpired

def test_state_cookie_roundtrip_and_expiry(monkeypatch):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-4 — sign/verify round-trip; cookie >5 min expired raises.
    """
    payload = {"state": "abc123", "code_verifier": "v" * 64}
    token = sign_state(payload)
    assert isinstance(token, str)
    assert verify_state(token) == payload

    # Simular expiración: itsdangerous URLSafeTimedSerializer permite forzar max_age.
    with pytest.raises(StateExpired):
        verify_state(token, max_age_seconds=0)
```

**GREEN:**
```python
"""Signed cookie for the temporary OAuth state + PKCE verifier."""
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

from ..config import settings


class StateInvalid(Exception): ...
class StateExpired(StateInvalid): ...


_SERIALIZER = URLSafeTimedSerializer(
    settings.jwt_secret.get_secret_value(),
    salt="oauth-state-v1",
)
DEFAULT_MAX_AGE = 300  # 5 min, per RF-AUTH-01 step 3


def sign_state(payload: dict) -> str:
    return _SERIALIZER.dumps(payload)


def verify_state(token: str, max_age_seconds: int = DEFAULT_MAX_AGE) -> dict:
    try:
        return _SERIALIZER.loads(token, max_age=max_age_seconds)
    except SignatureExpired as e:
        raise StateExpired() from e
    except BadSignature as e:
        raise StateInvalid() from e
```

**Commits:**
- `test(auth): SPEC-capa2 AC-4 — state cookie sign/verify with TTL`
- `feat(auth): SPEC-capa2 AC-4 — state cookie helper via itsdangerous`

---

### Task T3 — Session JWT (AC-5)

**Source:** SPEC-capa2-auth-msentra-v1, AC-5
**Criterion:** JWT HS256 con `JWT_SECRET`, payload {sub, oid, email, iat, exp}; firma inválida y exp respetadas.

**Files:**
- Test: `tests/unit/auth/test_session_jwt.py::test_session_jwt_roundtrip_and_validation`
- Impl: `src/transcription_api/auth/session.py`

**RED:**
```python
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from transcription_api.auth.session import (
    create_session_token, decode_session_token, SessionInvalid, SessionExpired,
)


def test_session_jwt_roundtrip_and_validation():
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-5 — JWT round-trip; bad signature raises; expiration respected.
    """
    user_id, oid = uuid.uuid4(), uuid.uuid4()
    token = create_session_token(user_id=user_id, ms_oid=oid, email="u@sandinas.test")
    claims = decode_session_token(token)
    assert claims["sub"] == str(user_id)
    assert claims["oid"] == str(oid)
    assert claims["email"] == "u@sandinas.test"

    # Tamper la firma
    tampered = token[:-4] + "AAAA"
    with pytest.raises(SessionInvalid):
        decode_session_token(tampered)

    # Token con exp en el pasado
    past_token = create_session_token(
        user_id=user_id, ms_oid=oid, email="u@sandinas.test",
        ttl_seconds=-1,  # ya expirado al firmar
    )
    with pytest.raises(SessionExpired):
        decode_session_token(past_token)
```

**GREEN:** módulo `auth/session.py` usando `authlib.jose.jwt`. Helpers `create_session_token`, `decode_session_token`, `set_session_cookie(response, token)`, `clear_session_cookie(response)`.

**Commits:**
- `test(auth): SPEC-capa2 AC-5 — session JWT round-trip + validation`
- `feat(auth): SPEC-capa2 AC-5 — session JWT helpers`

---

### Task T4 — Wire `auth.router` placeholder + main.py integration (AC-1 cierre)

**Source:** SPEC-capa2-auth-msentra-v1, AC-1
**Criterion:** auth router incluido en main app; AC-1 test sigue verde.

**Files:**
- Impl: `src/transcription_api/auth/routes.py` (esqueleto), `src/transcription_api/main.py` (include router).

**GREEN-only** (no RED separado, AC-1 ya tiene test): crear `routes.py` con `router = APIRouter(prefix="/auth", tags=["auth"])` y un endpoint placeholder `@router.get("/_ping")` que retorna `{"ok": True}`. Wire en main.py: `app.include_router(auth_router)`. AC-1 ya pasaba a nivel módulo; este task asegura el wire HTTP.

**Commit:** `feat(api): SPEC-capa2 AC-1 — wire auth router on main app`

---

**END BATCH 1 — STOP, report, await "continue".**

---

### Task T5 — `GET /auth/login` redirect a MS con PKCE (AC-6)

**Source:** SPEC-capa2-auth-msentra-v1, AC-6
**Criterion:** redirect 302 a `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/authorize?...` con `client_id`, `redirect_uri`, `state`, `code_challenge`, `code_challenge_method=S256`, `scope=openid profile email User.Read`. Cookie `oauth_state` HttpOnly Secure seteada. Llamadas consecutivas → states distintos.

**Files:**
- Test: `tests/integration/auth/test_login_flow.py::{test_login_redirects_to_ms_with_pkce, test_login_generates_unique_state_each_call}`
- Impl: `src/transcription_api/auth/oauth_client.py` (build_authorize_url + generate_pkce), `auth/routes.py` (GET /auth/login).

**RED:**
```python
import pytest
from urllib.parse import urlparse, parse_qs

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client():
    from transcription_api.main import app
    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
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
    assert "client_id" in qs and qs["client_id"][0]
    assert qs["response_type"] == ["code"]
    assert qs["redirect_uri"][0]
    assert qs["scope"][0] == "openid profile email User.Read"
    assert len(qs["state"][0]) >= 32
    assert len(qs["code_challenge"][0]) >= 43
    assert qs["code_challenge_method"] == ["S256"]
    # Cookie state seteada
    assert "oauth_state" in r.headers.get("set-cookie", "")
    assert "HttpOnly" in r.headers["set-cookie"]
    assert "Secure" in r.headers["set-cookie"]


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
```

**GREEN:**
- `oauth_client.py::generate_pkce()`: `verifier = secrets.token_urlsafe(64)`, `challenge = b64url(sha256(verifier))`.
- `oauth_client.py::build_authorize_url(state, code_challenge) -> str`.
- `routes.py::login()`: si cookie session válida → 302 `/mcp-setup`; sino genera state + verifier, sign_state, set_cookie, build_authorize_url, retorna 302.

**Commits:**
- `test(auth): SPEC-capa2 AC-6 — /auth/login redirects with PKCE`
- `feat(auth): SPEC-capa2 AC-6 — /auth/login route + oauth_client.build_authorize_url`

---

### Task T6 — `GET /auth/login` con sesión activa (AC-7)

**Source:** SPEC-capa2-auth-msentra-v1, AC-7
**Criterion:** user logueado → 302 directo a `/mcp-setup` sin tocar cookie state.

**Files:**
- Test: `tests/integration/auth/test_login_flow.py::test_login_with_session_skips_to_mcp_setup`

**RED:**
```python
async def test_login_with_session_skips_to_mcp_setup(client, session):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-7 — user con cookie session válida salta directo a /mcp-setup.
    """
    from tests.factories import make_user
    from transcription_api.auth.session import create_session_token
    user = await make_user(session)
    await session.commit()

    token = create_session_token(user_id=user.id, ms_oid=user.microsoft_oid, email=user.email)
    r = await client.get("/auth/login", cookies={"session": token})
    assert r.status_code == 302
    assert r.headers["location"] == "/mcp-setup"
    # NO se setea cookie oauth_state cuando ya hay sesión
    assert "oauth_state" not in r.headers.get("set-cookie", "")
```

**GREEN:** Logic in `routes.py::login()` ya planeado en T5; solo agregar el branch "if session valid".

**Commit:** `test(auth): SPEC-capa2 AC-7 — /auth/login skips when authenticated`

---

**END BATCH 2 — STOP, report, await "continue".**

---

### Task T7 — Callback primer login (AC-8)

**Source:** SPEC-capa2-auth-msentra-v1, AC-8
**Criterion:** callback con state + code válidos crea user, oauth_tokens (encrypted), mcp_bearers, set flash + session cookies, redirect a /mcp-setup.

**Files:**
- Test: `tests/integration/auth/test_callback_flow.py::test_callback_first_login_creates_all_rows`
- Impl: `auth/oauth_client.py` (exchange_code + JWKS validation), `auth/routes.py::callback()`, `auth/mcp_bearer.py`, `auth/flash.py`

**RED — pseudocode** (full implementation in GREEN; test usa `respx` para mockear MS):
```python
import respx
import httpx
from authlib.jose import jwt as jose_jwt

@respx.mock
async def test_callback_first_login_creates_all_rows(client, session, monkeypatch):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-8 — primer login crea user + oauth_tokens + mcp_bearers,
    setea flash y session cookies, redirige a /mcp-setup.
    """
    # Setup respx mocks para /token y /jwks
    fake_jwk = ...  # genera RS256 keypair test, expone JWKS
    fake_id_token = jose_jwt.encode({...claims con tid match...})
    respx.post(re.compile(r"login\.microsoftonline\.com.*/oauth2/v2.0/token")).respond(
        json={"access_token": "fake-access", "refresh_token": "fake-refresh",
              "id_token": fake_id_token, "expires_in": 3600}
    )
    respx.get(re.compile(r"login\.microsoftonline\.com.*/keys")).respond(json=fake_jwks)

    # 1) login → recibe cookie state + redirect URL
    login_r = await client.get("/auth/login")
    cookies = {"oauth_state": login_r.cookies["oauth_state"]}
    state = parse_qs(urlparse(login_r.headers["location"]).query)["state"][0]

    # 2) callback
    r = await client.get(f"/auth/callback?code=fake-code&state={state}", cookies=cookies)
    assert r.status_code == 302
    assert r.headers["location"] == "/mcp-setup"
    set_cookies = r.headers.get_list("set-cookie")
    assert any("session=" in c for c in set_cookies)
    assert any("mcp_bearer_flash=" in c for c in set_cookies)

    # DB state
    from transcription_api.db.models import User, OAuthToken, McpBearer
    from sqlalchemy import select
    users = (await session.execute(select(User))).scalars().all()
    assert len(users) == 1
    tokens = (await session.execute(select(OAuthToken))).scalars().all()
    assert len(tokens) == 1
    bearers = (await session.execute(select(McpBearer))).scalars().all()
    assert len(bearers) == 1 and bearers[0].revoked_at is None
```

**GREEN:**
- `oauth_client.exchange_code(code, code_verifier) -> dict`
- `oauth_client.fetch_jwks() -> dict` con cache 24h
- `oauth_client.validate_id_token(token, jwks) -> claims` con `aud`, `iss`, `exp`, `nbf`, `tid` validation
- `mcp_bearer.generate_bearer() -> (plaintext, sha256_hash)`
- `flash.set_bearer_flash(response, plaintext)`
- `routes.callback()`: orquesta el flow completo

**Commits:**
- `test(auth): SPEC-capa2 AC-8 — callback first login`
- `feat(auth): SPEC-capa2 AC-8 — callback orchestration + oauth_client + mcp_bearer + flash`

---

### Task T8 — Callback subsiguiente (AC-9)

**Source:** SPEC-capa2-auth-msentra-v1, AC-9
**Criterion:** user existente con misma `microsoft_oid` → UPDATE last_login + UPDATE oauth_tokens; **NO** se crea nuevo `mcp_bearers`.

**Files:**
- Test: `tests/integration/auth/test_callback_flow.py::test_callback_subsequent_login_updates_only`

**RED:** crear user + bearer existente vía factory antes del test; mock respx; GET /auth/callback; assert que count(users) == 1 (no duplicado), `users.last_login_at` avanzó, count(mcp_bearers WHERE revoked_at IS NULL) == 1 (no se creó otro).

**GREEN:** `routes.callback()` ya tiene lógica `if not exists -> insert; else -> update`. T8 confirma el branch else funciona.

**Commit:** `test(auth): SPEC-capa2 AC-9 — callback subsequent login updates only`

---

### Task T9 — Tokens MS encriptados verificable (AC-19)

**Source:** SPEC-capa2-auth-msentra-v1, AC-19
**Criterion:** tras callback, `SELECT ms_access_token_encrypted, ms_refresh_token_encrypted FROM oauth_tokens` retorna BYTEA que no contiene el plaintext mockeado.

**Files:**
- Test: `tests/integration/auth/test_callback_flow.py::test_oauth_tokens_stored_encrypted_not_plaintext`

**RED:**
```python
@respx.mock
async def test_oauth_tokens_stored_encrypted_not_plaintext(client, session):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-19 — los BYTEA en oauth_tokens NO contienen el plaintext.
    """
    plaintext_access = "PLAINTEXT_ACCESS_TOKEN_MARKER_xyz"
    plaintext_refresh = "PLAINTEXT_REFRESH_TOKEN_MARKER_abc"
    # mock /token para retornar estos valores
    ...
    # ejecutar callback exitoso
    ...
    # query directa
    from transcription_api.db.models import OAuthToken
    tok = (await session.execute(select(OAuthToken))).scalar_one()
    assert plaintext_access.encode() not in tok.ms_access_token_encrypted
    assert plaintext_refresh.encode() not in tok.ms_refresh_token_encrypted
    # verificar que decrypt los recupera
    from transcription_api.auth.crypto import decrypt_token
    assert decrypt_token(tok.ms_access_token_encrypted) == plaintext_access
    assert decrypt_token(tok.ms_refresh_token_encrypted) == plaintext_refresh
```

**GREEN:** ya implementado en T7 (callback usa `encrypt_token` antes de INSERT). Este test confirma la propiedad.

**Commit:** `test(auth): SPEC-capa2 AC-19 — oauth tokens stored encrypted (no plaintext leak)`

---

**END BATCH 3 — STOP, report, await "continue".**

---

### Task T10 — Callback state mismatch (AC-10)

**Source:** SPEC-capa2-auth-msentra-v1, AC-10 (RF-AUTH-02 ERR AUTH_INVALID_STATE)
**Criterion:** cookie state.state != query.state → 302 a `/login?error=AUTH_INVALID_STATE`. NO se crea row.

**RED:** GET /auth/callback con cookie state firmada para `state="abc"` y query `state=xyz`. Assert 302, Location matches `/login?error=AUTH_INVALID_STATE`. SELECT count users == 0.

**GREEN:** error handler en routes.callback() + `auth/errors.py::AuthError` con `redirect_to`.

**Commit:** `test(auth): SPEC-capa2 AC-10 — state mismatch redirects with error`

---

### Task T11 — Tenant rechazado (AC-11)

**Source:** SPEC-capa2-auth-msentra-v1, AC-11 (RF-AUTH-03)
**Criterion:** id_token con `tid != MS_TENANT_ID` → 302 a `/login?error=AUTH_TENANT_NOT_ALLOWED`. NO se crea user.

**RED:** mock /token retorna id_token con `tid="foreign-tenant"`. Assert 302 con error code, count(users) == 0.

**GREEN:** `oauth_client.validate_id_token` ya valida `tid`; el AuthError handler mapea a redirect.

**Commit:** `test(auth): SPEC-capa2 AC-11 — foreign tenant rejected`

---

### Task T12 — MS unavailable (AC-12)

**Source:** SPEC-capa2-auth-msentra-v1, AC-12 (RF-AUTH-05)
**Criterion:** `respx` retorna 503 al /token → 302 a `/login?error=AUTH_PROVIDER_UNAVAILABLE`. Log emitido.

**RED:** `respx.post(...).respond(status_code=503)`. Assert 302 + error code.

**GREEN:** wrap exchange_code en try/except con timeout 10s. Capturar HTTPStatusError 5xx + httpx.TimeoutException → AuthError(AUTH_PROVIDER_UNAVAILABLE).

**Commit:** `test(auth): SPEC-capa2 AC-12 — MS unavailable maps to AUTH_PROVIDER_UNAVAILABLE`

---

**END BATCH 4 — STOP, report, await "continue".**

---

### Task T13 — `GET /auth/me` con flash (AC-13)

**Source:** SPEC-capa2-auth-msentra-v1, AC-13 (RF-AUTH-06)
**Criterion:** user logueado + cookie `mcp_bearer_flash` presente. Response 200 con bearer.plaintext + bearer.id + mcp_url. Cookie flash borrada en response.

**Files:**
- Test: `tests/integration/auth/test_me_endpoint.py::test_me_with_flash_returns_plaintext_and_clears`
- Impl: `auth/routes.py::me()`, `auth/dependencies.py::get_current_user_web`, `auth/flash.pop_bearer_flash`

**RED + GREEN:** ver pattern de T7. dependency parsea cookie session via `decode_session_token`, lookup user, lee+borra flash, retorna JSON.

**Commits:**
- `test(auth): SPEC-capa2 AC-13 — /auth/me with flash`
- `feat(auth): SPEC-capa2 AC-13 — /auth/me + get_current_user_web + flash pop`

---

### Task T14 — `GET /auth/me` sin flash (AC-14)

**Source:** SPEC-capa2-auth-msentra-v1, AC-14
**Criterion:** logueado, sin flash → 200 con bearer.plaintext null.

**RED:** GET /auth/me con cookie session pero sin flash. Assert `body.bearer.plaintext is None and body.bearer.id is not None`.

**GREEN:** lógica en routes.me() ya de T13.

**Commit:** `test(auth): SPEC-capa2 AC-14 — /auth/me without flash returns id only`

---

### Task T15 — `/auth/me` no autenticado (AC-15)

**Source:** SPEC-capa2-auth-msentra-v1, AC-15
**Criterion:** sin cookie session → 401 + `error_code: AUTH_NOT_AUTHENTICATED`.

**RED:** GET /auth/me sin cookies. Assert 401, body matches.

**GREEN:** `get_current_user_web` raise AuthError(AUTH_NOT_AUTHENTICATED, http_status=401, redirect_to=None).

**Commit:** `test(auth): SPEC-capa2 AC-15 — /auth/me unauthenticated returns 401`

---

### Task T16 — `/auth/regenerate-mcp-token` (AC-16, ERR-4)

**Source:** SPEC-capa2-auth-msentra-v1, AC-16 + ERR-4 (RF-AUTH-07)
**Criterion:** revoca activo + emite nuevo. Race con doble request → partial UNIQUE catch + retry.

**Files:**
- Test: `tests/integration/auth/test_regenerate_endpoint.py::{test_regenerate_revokes_old_and_issues_new, test_regenerate_handles_race_via_partial_unique}`
- Impl: `auth/routes.py::regenerate_mcp_token`

**RED+GREEN:** logic en routes: tx con UPDATE revoked + INSERT new. Catch IntegrityError (constraint partial UNIQUE) → revoke_active(user) + retry una vez.

**Commits:**
- `test(auth): SPEC-capa2 AC-16+ERR-4 — regenerate revokes old + handles race`
- `feat(auth): SPEC-capa2 AC-16+ERR-4 — regenerate route with race handling`

---

### Task T17 — Logout (AC-20, ALT-1)

**Source:** SPEC-capa2-auth-msentra-v1, AC-20 + ALT-1
**Criterion:** logout borra session cookie; subsiguiente /auth/me → 401. Bearer MCP NO se revoca (sigue válido).

**Files:**
- Test: `tests/integration/auth/test_me_endpoint.py::{test_logout_clears_session_cookie, test_logout_does_not_revoke_mcp_bearer}`
- Impl: `auth/routes.py::logout`

**RED:**
```python
async def test_logout_clears_session_cookie(client, session):
    # ...login first via test factory + session.create_session_token
    r = await client.post("/auth/logout", cookies={"session": token})
    assert r.status_code == 302
    assert "session=" in r.headers.get("set-cookie", "") and "Max-Age=0" in r.headers["set-cookie"]
    # subsiguiente /auth/me sin nueva cookie
    r2 = await client.get("/auth/me")
    assert r2.status_code == 401


async def test_logout_does_not_revoke_mcp_bearer(client, session):
    """ALT-1: logout NO revoca el bearer MCP."""
    # crear user + bearer activo, logear, logout
    # luego verificar que bearer activo sigue en DB con revoked_at IS NULL
```

**GREEN:** `routes.logout`: clear_session_cookie + 302 a /login. NO toca mcp_bearers.

**Commits:**
- `test(auth): SPEC-capa2 AC-20+ALT-1 — logout clears web session, leaves MCP bearer`
- `feat(auth): SPEC-capa2 AC-20+ALT-1 — /auth/logout`

---

**END BATCH 5 — STOP, report, await "continue".**

---

### Task T18 — `get_current_user_mcp` middleware (AC-17)

**Source:** SPEC-capa2-auth-msentra-v1, AC-17
**Criterion:** dependency parsea `Authorization: Bearer <plaintext>`, lookup en `mcp_bearers WHERE token_hash = sha256(plaintext) AND revoked_at IS NULL`, retorna User. Setea `db.info["user_id"] = user.id`. Sin Authorization → 401. Bearer revoked → 401. Bearer inexistente → 401.

**Files:**
- Test: `tests/integration/auth/test_mcp_bearer_middleware.py::test_mcp_bearer_middleware` (parametrize)
- Impl: `auth/dependencies.py::get_current_user_mcp`, test route `/_test_mcp_protected`

**RED:**
```python
@pytest.mark.parametrize("scenario,header,expected_status", [
    ("no_header", None, 401),
    ("malformed", "NotBearer xyz", 401),
    ("missing_bearer", "Bearer ", 401),
    ("revoked_bearer", "Bearer <revoked-plaintext>", 401),
    ("nonexistent", "Bearer nosuchtoken", 401),
    ("valid", "Bearer <valid-plaintext>", 200),
])
async def test_mcp_bearer_middleware(client, session, scenario, header, expected_status):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-17 — get_current_user_mcp dependency.
    """
    # setup user + bearer activo + bearer revoked vía factories
    # llamar /_test_mcp_protected
    # validar status + (si 200) que session.info[user_id] quedó seteado
```

**GREEN:** `dependencies.get_current_user_mcp(authorization: str = Header(None), db: AsyncSession = Depends(get_session))`:
- parse Bearer, sha256 hash, query mcp_bearers join users, set db.info["user_id"], retorna User.

Crear test route `app.get("/_test_mcp_protected")` que use la dependency.

**Commits:**
- `test(auth): SPEC-capa2 AC-17 — MCP bearer dependency parametrized`
- `feat(auth): SPEC-capa2 AC-17 — get_current_user_mcp + verify_bearer + scoping activation`

---

### Task T19 — Cross-user isolation via MCP bearer (AC-18)

**Source:** SPEC-capa2-auth-msentra-v1, AC-18
**Criterion:** dos users A y B con bearers distintos. User A llama route que `select(Transcription)` retorna solo data de A; User B análogo. Confirma que MCP middleware activa ADR-014.

**Files:**
- Test: `tests/integration/auth/test_mcp_bearer_middleware.py::test_mcp_bearer_activates_per_user_scoping`

**RED:**
```python
async def test_mcp_bearer_activates_per_user_scoping(client, session):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-18 — autenticación MCP activa el listener S-1 (ADR-014).

    Setup: 2 users con sus respectivos bearers + transcripciones propias.
    Call: cada user llama una test route que hace select(Transcription) sin
    WHERE clause. La query debe retornar SOLO sus rows.
    """
    from tests.factories import make_user, make_bearer, make_transcription
    user_a = await make_user(session, email="a@x")
    user_b = await make_user(session, email="b@x")
    bearer_a_pt, bearer_a_hash = generate_bearer()
    bearer_b_pt, bearer_b_hash = generate_bearer()
    await make_bearer(session, user_id=user_a.id, token_hash=bearer_a_hash)
    await make_bearer(session, user_id=user_b.id, token_hash=bearer_b_hash)
    await make_transcription(session, user_id=user_a.id, audio_hash="ha")
    await make_transcription(session, user_id=user_b.id, audio_hash="hb")
    await session.commit()

    # Test route que hace SELECT * FROM transcriptions sin filter
    @app.get("/_test_user_data")
    async def _route(user=Depends(get_current_user_mcp), db=Depends(get_session)):
        from transcription_api.db.models import Transcription
        rows = (await db.execute(select(Transcription))).scalars().all()
        return {"hashes": [r.audio_hash for r in rows]}

    rA = await client.get("/_test_user_data", headers={"Authorization": f"Bearer {bearer_a_pt}"})
    rB = await client.get("/_test_user_data", headers={"Authorization": f"Bearer {bearer_b_pt}"})
    assert rA.json()["hashes"] == ["ha"]
    assert rB.json()["hashes"] == ["hb"]
```

**GREEN:** la integración entre `get_current_user_mcp` (T18) y `enable_per_user_scoping` (Capa 1, ya activo) ya está hecha — el listener S-1 aplica `WHERE user_id=X` cuando `db.info["user_id"]` está seteado. Este test es el invariant probe end-to-end de la cadena bearer→scoping.

**Commit:** `test(auth): SPEC-capa2 AC-18 — MCP bearer activates per-user scoping (ADR-014 e2e)`

---

**END BATCH 6 — final report, run /trazabilidad.**

---

## Traceability Matrix

| Spec | Criterion | Test Function | Status |
|---|---|---|---|
| SPEC-capa2 | AC-1 | `test_auth_module_imports` | [ ] |
| SPEC-capa2 | AC-2 | `test_crypto_roundtrip_unique_nonce` | [ ] |
| SPEC-capa2 | AC-3 | `test_crypto_tamper_raises_invalid_tag` | [ ] |
| SPEC-capa2 | AC-4 | `test_state_cookie_roundtrip_and_expiry` | [ ] |
| SPEC-capa2 | AC-5 | `test_session_jwt_roundtrip_and_validation` | [ ] |
| SPEC-capa2 | AC-6 | `test_login_redirects_to_ms_with_pkce` + `test_login_generates_unique_state_each_call` | [ ] |
| SPEC-capa2 | AC-7 | `test_login_with_session_skips_to_mcp_setup` | [ ] |
| SPEC-capa2 | AC-8 | `test_callback_first_login_creates_all_rows` | [ ] |
| SPEC-capa2 | AC-9 | `test_callback_subsequent_login_updates_only` | [ ] |
| SPEC-capa2 | AC-10 | `test_callback_state_mismatch_redirects_with_error` | [ ] |
| SPEC-capa2 | AC-11 | `test_callback_foreign_tenant_redirects_with_error` | [ ] |
| SPEC-capa2 | AC-12 | `test_callback_ms_unavailable_redirects_with_error` | [ ] |
| SPEC-capa2 | AC-13 | `test_me_with_flash_returns_plaintext_and_clears` | [ ] |
| SPEC-capa2 | AC-14 | `test_me_without_flash_returns_id_only` | [ ] |
| SPEC-capa2 | AC-15 | `test_me_unauthenticated_returns_401` | [ ] |
| SPEC-capa2 | AC-16 | `test_regenerate_revokes_old_and_issues_new` | [ ] |
| SPEC-capa2 | AC-17 | `test_mcp_bearer_middleware` (×6 parametrize) | [ ] |
| SPEC-capa2 | AC-18 | `test_mcp_bearer_activates_per_user_scoping` | [ ] |
| SPEC-capa2 | AC-19 | `test_oauth_tokens_stored_encrypted_not_plaintext` | [ ] |
| SPEC-capa2 | AC-20 | `test_logout_clears_session_cookie` | [ ] |
| SPEC-capa2 | ERR-1 | (cubierto en doc; TTL JWKS = 24h verificable manualmente) | [ ] |
| SPEC-capa2 | ERR-2 | `test_invalid_enc_key_fails_loud` | [ ] |
| SPEC-capa2 | ERR-3 | (validación en `Settings`; cubierto por inspección manual del config) | [ ] |
| SPEC-capa2 | ERR-4 | `test_regenerate_handles_race_via_partial_unique` | [ ] |
| SPEC-capa2 | ERR-5 | doc-only (race en re-login es comportamiento documentado, no bug) | [ ] |
| SPEC-capa2 | ALT-1 | `test_logout_does_not_revoke_mcp_bearer` | [ ] |
| SPEC-capa2 | ALT-2 | doc-only (separación deliberada de dependencies) | [ ] |
| SPEC-capa2 | ALT-3 | doc-only (logout global diferido a Capa 7) | [ ] |

**Coverage target:** 25/28 in-scope items con test (3 son doc-only por diseño). 100% de los AC tienen test.

---

## Completion Checklist

- [ ] Todos los 20 ACs tienen al menos 1 test pasando.
- [ ] 4 ERR/ALT in-scope con cobertura explícita (ERR-2, ERR-4, ALT-1, AC-12 cubre ERR-5).
- [ ] `pytest tests/` exits 0 — los 95 tests de Capa 1 deben seguir verde.
- [ ] No leak de plaintext en oauth_tokens (AC-19 explícito).
- [ ] Cross-user isolation verificada con bearer MCP (AC-18 — la prueba más importante de Capa 2).
- [ ] Todos los commits con formato `<type>(<scope>): SPEC-capa2 <AC-id> — <desc>`.
- [ ] Traceability matrix actualizada a `[x]`.
- [ ] Tras completion: `sandinas-wiki-skills:ps-trazabilidad` para verificar wiki ↔ código (los RFs ya existen, solo se confirma que cada RF tiene su TP correspondiente implementado).

## Squash / Capa-close Message Template

```
feat(capa2): SPEC-capa2-auth-msentra-v1 — MS Entra OAuth + auth endpoints + MCP bearer

Implementa:
- Flow OAuth 2.0 con PKCE contra Microsoft Entra ID (RF-AUTH-01..05).
- Endpoints /auth/login, /auth/callback, /auth/me, /auth/regenerate-mcp-token,
  /auth/logout (RF-AUTH-06+07 + ALT-1 logout web-only).
- AES-256-GCM para encriptación de tokens MS en oauth_tokens BYTEA.
- HS256 JWT para cookie session web (TTL 24h).
- itsdangerous para cookie state temporal (TTL 5min).
- Dependency get_current_user_mcp que activa per-user scoping (ADR-014).

Tests: 28 nuevos + 95 de Capa 1 = 123 passing.
Spec: docs/sesiones/2026-05-05-capa2-auth-spec.md (89f0a61)
Traceability: 25/28 in-scope items con test, 3 doc-only.
```
