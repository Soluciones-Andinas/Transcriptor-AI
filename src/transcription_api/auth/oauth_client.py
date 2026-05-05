"""OAuth client helpers for Microsoft Entra ID.

Spec: SPEC-capa2-auth-msentra-v1
- RF-AUTH-01 step 4: build the exact authorize URL Microsoft expects.
- RF-AUTH-02 steps 3-4: exchange_code (POST /token with client_secret + verifier).
- RF-AUTH-02 step 5 / RF-AUTH-03: validate_id_token with JWKS-fetched public keys.

httpx.AsyncClient is reused across calls. JWKS is cached with TTL 24h
(`_JWKS_CACHE`); on signature failures the caller should evict the cache
once and retry (handles MS key rotation).

Capa 2 review:
- H-5: `_JWKS_FETCH_LOCK` (asyncio.Lock) serializes concurrent JWKS
  refreshes so a thundering herd at TTL expiry only triggers ONE outbound
  request to login.microsoftonline.com. Subsequent waiters re-check the
  cache after acquiring the lock (double-checked locking) and return the
  fresh value without a second fetch.
- M-5: removed the dead `[:96]` slice on the PKCE verifier.
  `secrets.token_urlsafe(64)` already returns ~86 chars (well under 96
  and well within the 43–128 RFC 7636 range), so the slice never trims
  anything.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey, jwt
from authlib.jose.errors import JoseError

from ..config import settings


# --- Errors --------------------------------------------------------------
class OAuthError(Exception):
    """Base class for oauth_client errors. Subtypes carry semantics."""


class OAuthCodeInvalid(OAuthError):
    """Microsoft returned 4xx on /token — bad code or expired."""


class OAuthProviderUnavailable(OAuthError):
    """Microsoft Entra timeout or 5xx — RF-AUTH-05."""


class IdTokenInvalid(OAuthError):
    """id_token failed validation: bad signature, missing/wrong claim."""


class TenantNotAllowed(IdTokenInvalid):
    """id_token.tid does not match MS_TENANT_ID — RF-AUTH-03."""


# --- PKCE + authorize URL (B2) -------------------------------------------
def generate_pkce() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) for OAuth 2.0 PKCE S256.

    `secrets.token_urlsafe(64)` returns ~86 url-safe chars, comfortably
    inside the RFC 7636 §4.1 verifier range of 43–128 chars. No truncation
    is needed.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(state: str, code_challenge: str) -> str:
    """RF-AUTH-01 step 4."""
    base = (
        f"https://login.microsoftonline.com/"
        f"{settings.ms_tenant_id}/oauth2/v2.0/authorize"
    )
    params = {
        "client_id": settings.ms_client_id,
        "response_type": "code",
        "redirect_uri": settings.ms_redirect_uri,
        "scope": "openid profile email User.Read",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{base}?{urlencode(params)}"


# --- Endpoints (B3) -------------------------------------------------------
def _token_endpoint() -> str:
    return f"https://login.microsoftonline.com/{settings.ms_tenant_id}/oauth2/v2.0/token"


def _jwks_endpoint() -> str:
    # MS publishes JWKS at the /discovery/v2.0/keys path of the tenant.
    return f"https://login.microsoftonline.com/{settings.ms_tenant_id}/discovery/v2.0/keys"


_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


async def exchange_code(code: str, code_verifier: str) -> dict[str, Any]:
    """POST /token with PKCE; return MS's JSON. Raises typed errors on failure."""
    payload = {
        "grant_type": "authorization_code",
        "client_id": settings.ms_client_id,
        "client_secret": settings.ms_client_secret.get_secret_value(),
        "code": code,
        "redirect_uri": settings.ms_redirect_uri,
        "code_verifier": code_verifier,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(_token_endpoint(), data=payload)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise OAuthProviderUnavailable(str(exc)) from exc

    if r.status_code >= 500:
        raise OAuthProviderUnavailable(f"MS returned {r.status_code}")
    if r.status_code >= 400:
        raise OAuthCodeInvalid(f"MS returned {r.status_code}: {r.text[:200]}")

    return r.json()


# --- JWKS cache ----------------------------------------------------------
_JWKS_CACHE: dict[str, Any] | None = None
_JWKS_CACHE_AT: float = 0.0
_JWKS_TTL_SECONDS = 24 * 3600
# H-5: serialize concurrent refreshes so a thundering herd at TTL expiry
# triggers exactly one outbound request to login.microsoftonline.com.
_JWKS_FETCH_LOCK: asyncio.Lock | None = None


def _jwks_lock() -> asyncio.Lock:
    """Lazily build the lock — at module import there may be no running loop."""
    global _JWKS_FETCH_LOCK
    if _JWKS_FETCH_LOCK is None:
        _JWKS_FETCH_LOCK = asyncio.Lock()
    return _JWKS_FETCH_LOCK


def _cache_is_fresh() -> bool:
    return (
        _JWKS_CACHE is not None
        and (time.time() - _JWKS_CACHE_AT) < _JWKS_TTL_SECONDS
    )


async def fetch_jwks(force_refresh: bool = False) -> dict[str, Any]:
    """Return the cached JWKS or fetch a fresh copy if expired or forced.

    Concurrent callers are serialized by `_JWKS_FETCH_LOCK`. The first
    waiter through the lock either confirms the cache is now fresh (a
    prior holder refreshed it) and returns it, or performs the fetch
    itself. This implements the standard double-checked-locking pattern.
    """
    global _JWKS_CACHE, _JWKS_CACHE_AT

    if not force_refresh and _cache_is_fresh():
        return _JWKS_CACHE  # type: ignore[return-value]

    async with _jwks_lock():
        # Re-check under the lock: a prior holder may have refreshed.
        if not force_refresh and _cache_is_fresh():
            return _JWKS_CACHE  # type: ignore[return-value]

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                r = await client.get(_jwks_endpoint())
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise OAuthProviderUnavailable(f"JWKS fetch failed: {exc}") from exc
        if r.status_code >= 500:
            raise OAuthProviderUnavailable(f"JWKS endpoint returned {r.status_code}")
        if r.status_code >= 400:
            raise IdTokenInvalid(f"JWKS endpoint returned {r.status_code}")

        _JWKS_CACHE = r.json()
        _JWKS_CACHE_AT = time.time()
        return _JWKS_CACHE


def _expected_issuer() -> str:
    return f"https://login.microsoftonline.com/{settings.ms_tenant_id}/v2.0"


def validate_id_token(id_token: str, jwks: dict[str, Any]) -> dict[str, Any]:
    """Decode + validate id_token against JWKS. Returns claims dict.

    Raises IdTokenInvalid on signature failure or claim violation, or the
    more specific TenantNotAllowed when `tid` does not match.
    """
    try:
        key_set = JsonWebKey.import_key_set(jwks)
        claims = jwt.decode(id_token, key=key_set)
        # Run standard validation (exp, nbf, iat).
        claims_options = {
            "iss": {"essential": True, "values": [_expected_issuer()]},
            "aud": {"essential": True, "values": [settings.ms_client_id]},
            "exp": {"essential": True},
        }
        claims.options = claims_options
        claims.validate()
    except JoseError as exc:
        raise IdTokenInvalid(str(exc)) from exc

    # RF-AUTH-03: tenant must match. authlib doesn't validate `tid` by default.
    tid = claims.get("tid")
    if tid != settings.ms_tenant_id:
        raise TenantNotAllowed(f"id_token.tid={tid!r} does not match MS_TENANT_ID")

    return dict(claims)


def reset_jwks_cache() -> None:
    """Test-only helper to invalidate the JWKS cache between cases.

    Also drops the asyncio.Lock so a fresh test can build it in its own
    event loop (asyncio.Lock is loop-bound on construction).
    """
    global _JWKS_CACHE, _JWKS_CACHE_AT, _JWKS_FETCH_LOCK
    _JWKS_CACHE = None
    _JWKS_CACHE_AT = 0.0
    _JWKS_FETCH_LOCK = None
