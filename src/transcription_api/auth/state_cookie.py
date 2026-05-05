"""Signed cookie for the temporary OAuth state + PKCE verifier.

Spec: SPEC-capa2-auth-msentra-v1
RF-AUTH-01 step 3: cookie `oauth_state` HttpOnly Secure SameSite=Lax with
TTL 5 min, signed with the JWT secret. Holds `{state, code_verifier}` so the
callback (RF-AUTH-02 step 1-2) can validate the state matches and recover
the PKCE verifier needed for token exchange.

`itsdangerous.URLSafeTimedSerializer` is preferred over a full JWT here
because the payload is short-lived and never sent to a third party — we
just need tamper-evidence + expiry, not the JWT claim ecosystem.
"""
from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..config import settings


class StateInvalid(Exception):
    """Cookie signature mismatch or malformed token."""


class StateExpired(StateInvalid):
    """Cookie was correctly signed but TTL exceeded."""


_SERIALIZER = URLSafeTimedSerializer(
    settings.jwt_secret.get_secret_value(),
    salt="oauth-state-v1",
)
DEFAULT_MAX_AGE = 300  # 5 min, per RF-AUTH-01 step 3


def sign_state(payload: dict) -> str:
    """Sign + encode {state, code_verifier} for the oauth_state cookie."""
    return _SERIALIZER.dumps(payload)


def verify_state(token: str, max_age_seconds: int = DEFAULT_MAX_AGE) -> dict:
    """Decode + verify; raises StateExpired or StateInvalid on failure."""
    try:
        return _SERIALIZER.loads(token, max_age=max_age_seconds)
    except SignatureExpired as exc:
        raise StateExpired() from exc
    except BadSignature as exc:
        raise StateInvalid() from exc
