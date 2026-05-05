"""Unit tests for `transcription_api.auth.session`.

Spec: SPEC-capa2-auth-msentra-v1
Plan: docs/sesiones/2026-05-05-capa2-auth-plan.md (T3)
Covers: AC-5 (session JWT round-trip + signature/expiry validation).
"""
from __future__ import annotations

import uuid

import pytest


def test_session_jwt_roundtrip_and_validation():
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-5 — JWT round-trip with sub/oid/email; tampered signature
    raises SessionInvalid; expired token raises SessionExpired.
    """
    from transcription_api.auth.session import (
        SessionExpired,
        SessionInvalid,
        create_session_token,
        decode_session_token,
    )

    user_id = uuid.uuid4()
    ms_oid = uuid.uuid4()
    token = create_session_token(user_id=user_id, ms_oid=ms_oid, email="u@sandinas.test")
    assert isinstance(token, str)

    claims = decode_session_token(token)
    assert claims["sub"] == str(user_id)
    assert claims["oid"] == str(ms_oid)
    assert claims["email"] == "u@sandinas.test"
    assert "iat" in claims and "exp" in claims and claims["exp"] > claims["iat"]

    # Tamper: flip the last 4 base64 chars of the signature.
    tampered = token[:-4] + "AAAA"
    with pytest.raises(SessionInvalid):
        decode_session_token(tampered)

    # Already-expired token: TTL of -1s puts exp before iat, so verification fails.
    expired = create_session_token(
        user_id=user_id, ms_oid=ms_oid, email="u@sandinas.test", ttl_seconds=-1,
    )
    with pytest.raises(SessionExpired):
        decode_session_token(expired)
