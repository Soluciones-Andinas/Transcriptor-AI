"""Unit tests for `transcription_api.auth.crypto`.

Spec: SPEC-capa2-auth-msentra-v1
Plan: docs/sesiones/2026-05-05-capa2-auth-plan.md (T1)
Covers: AC-2 (round-trip + nonce randomness), AC-3 (tamper detection),
ERR-2 (invalid OAUTH_TOKEN_ENC_KEY fails loud at module load).
"""
from __future__ import annotations

import importlib

import pytest
from cryptography.exceptions import InvalidTag


def test_crypto_roundtrip_unique_nonce():
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-2 — encrypt/decrypt round-trip; same plaintext produces
    different ciphertexts (nonce random per call).
    """
    from transcription_api.auth.crypto import decrypt_token, encrypt_token

    plaintext = "ya29.a0AfH6SMBexample-access-token-from-microsoft"
    ct1 = encrypt_token(plaintext)
    ct2 = encrypt_token(plaintext)
    assert isinstance(ct1, bytes) and len(ct1) > 12  # at least nonce + ct
    assert ct1 != ct2  # nonce random => ciphertext distinto
    assert decrypt_token(ct1) == plaintext
    assert decrypt_token(ct2) == plaintext


def test_crypto_tamper_raises_invalid_tag():
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-3 — modificar 1 byte del ciphertext debe raise InvalidTag.
    """
    from transcription_api.auth.crypto import decrypt_token, encrypt_token

    ct = bytearray(encrypt_token("secret"))
    ct[-1] ^= 0xFF  # flip ultimo byte (parte del tag)
    with pytest.raises(InvalidTag):
        decrypt_token(bytes(ct))


def test_invalid_enc_key_fails_loud(monkeypatch):
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Error: ERR-2 — OAUTH_TOKEN_ENC_KEY que no es 32 bytes raise al import,
    no en el primer encrypt en runtime.
    """
    from pydantic import SecretStr

    from transcription_api.config import settings

    # Forzar key invalida
    monkeypatch.setattr(settings, "oauth_token_enc_key", SecretStr("too-short"))
    # El modulo debe rechazar la key cuando se cargue el AESGCM cipher.
    from transcription_api.auth import crypto

    with pytest.raises((ValueError, RuntimeError)):
        importlib.reload(crypto)
