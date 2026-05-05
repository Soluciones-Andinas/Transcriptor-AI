"""Session JWT for the web cookie set after successful login.

Spec: SPEC-capa2-auth-msentra-v1
RF-AUTH-02 paso 9 + "Closed decisions": JWT firmado HS256 con `JWT_SECRET`,
TTL `SESSION_TTL_SECONDS` (default 86400 = 24h), payload mínimo:
    {sub: user.id, oid: user.microsoft_oid, email, iat, exp}

`authlib.jose` provides JWT (RFC 7519) with proper header `typ: JWT`,
signature validation, and exp/nbf claim checks. We do NOT include sensitive
claims (no access_token, no refresh_token); those live encrypted in the
`oauth_tokens` table.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from authlib.jose import JoseError, JsonWebToken
from authlib.jose.errors import ExpiredTokenError

from ..config import settings


class SessionInvalid(Exception):
    """JWT signature mismatch, malformed token, or claim validation failure."""


class SessionExpired(SessionInvalid):
    """JWT was signed correctly but `exp` claim is in the past."""


_JWT = JsonWebToken(["HS256"])
_HEADER = {"alg": "HS256", "typ": "JWT"}


def _secret() -> bytes:
    return settings.jwt_secret.get_secret_value().encode("utf-8")


def create_session_token(
    *,
    user_id: uuid.UUID,
    ms_oid: uuid.UUID,
    email: str,
    ttl_seconds: int | None = None,
) -> str:
    """Sign a JWT with the user's identity. TTL defaults to settings.session_ttl_seconds."""
    if ttl_seconds is None:
        ttl_seconds = settings.session_ttl_seconds
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "oid": str(ms_oid),
        "email": email,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    token_bytes = _JWT.encode(_HEADER, payload, _secret())
    return token_bytes.decode("utf-8")


def decode_session_token(token: str) -> dict[str, Any]:
    """Validate signature + exp; return claims. Raises SessionInvalid / SessionExpired."""
    try:
        claims = _JWT.decode(token, _secret())
        # `validate` runs the standard claim checks (exp, nbf, iat, etc.).
        claims.validate()
        return dict(claims)
    except ExpiredTokenError as exc:
        raise SessionExpired() from exc
    except JoseError as exc:
        raise SessionInvalid() from exc
