"""AES-256-GCM encryption for OAuth tokens stored in oauth_tokens.ms_*_encrypted.

Spec: SPEC-capa2-auth-msentra-v1
Closed decision (RF-AUTH-02): AES-256-GCM with OAUTH_TOKEN_ENC_KEY.

The key is read once at module import time. Output layout:

    nonce (12 bytes) || ciphertext || gcm_tag (16 bytes appended by AESGCM.encrypt)

Tampering with any byte raises `cryptography.exceptions.InvalidTag` on decrypt.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import settings


def _load_key() -> bytes:
    raw = settings.oauth_token_enc_key.get_secret_value()
    if not raw:
        raise RuntimeError(
            "OAUTH_TOKEN_ENC_KEY is empty. Set it to urlsafe-b64 of 32 bytes; "
            "generate with: python -c 'import secrets, base64; "
            "print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'"
        )
    try:
        # Accept urlsafe-b64; pad if missing.
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as exc:
        raise RuntimeError(
            f"OAUTH_TOKEN_ENC_KEY is not valid urlsafe-base64: {exc}"
        ) from exc
    if len(key) != 32:
        raise RuntimeError(
            f"OAUTH_TOKEN_ENC_KEY must decode to 32 bytes, got {len(key)} bytes"
        )
    return key


_AESGCM = AESGCM(_load_key())


def encrypt_token(plaintext: str) -> bytes:
    """Encrypt with AES-256-GCM. Output: 12-byte nonce || ciphertext || 16-byte tag."""
    nonce = os.urandom(12)
    ct = _AESGCM.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    return nonce + ct


def decrypt_token(ciphertext: bytes) -> str:
    """Decrypt; raises cryptography.exceptions.InvalidTag on tamper or wrong key."""
    nonce, ct = ciphertext[:12], ciphertext[12:]
    pt = _AESGCM.decrypt(nonce, ct, associated_data=None)
    return pt.decode("utf-8")
