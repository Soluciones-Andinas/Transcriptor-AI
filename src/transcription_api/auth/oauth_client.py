"""OAuth client helpers for Microsoft Entra ID.

Spec: SPEC-capa2-auth-msentra-v1
RF-AUTH-01 step 4: build the exact authorize URL Microsoft expects.
RF-AUTH-02 step 3+: exchange_code (deferred to Batch 3, just signature here).
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

from ..config import settings


def generate_pkce() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) for OAuth 2.0 PKCE S256.

    Returns a tuple where:
    - verifier: 43-128 char URL-safe random string (RFC 7636 §4.1).
    - challenge: base64url-encoded SHA-256 of the verifier, no padding.
    """
    verifier = secrets.token_urlsafe(64)[:96]  # ~96 chars, well within 43-128
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(state: str, code_challenge: str) -> str:
    """Construct the Microsoft Entra authorization URL per RF-AUTH-01 step 4."""
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
