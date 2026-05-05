"""Unit tests for `transcription_api.auth.state_cookie`.

Spec: SPEC-capa2-auth-msentra-v1
Plan: docs/sesiones/2026-05-05-capa2-auth-plan.md (T2)
Covers: AC-4 (state cookie sign/verify with TTL).
"""
from __future__ import annotations

import pytest


def test_state_cookie_roundtrip_and_expiry():
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-4 — sign/verify roundtrip; cookie >5 min raises StateExpired.
    """
    from transcription_api.auth.state_cookie import (
        StateExpired,
        StateInvalid,
        sign_state,
        verify_state,
    )

    payload = {"state": "abc123", "code_verifier": "v" * 64}
    token = sign_state(payload)
    assert isinstance(token, str)
    assert verify_state(token) == payload

    # max_age=-1 forces expiry: itsdangerous compares `age > max_age` strictly,
    # so within the same second age=0 and 0 > 0 is False (max_age=0 won't fire).
    # max_age=-1 makes 0 > -1 True → SignatureExpired → wrapped as StateExpired.
    with pytest.raises(StateExpired):
        verify_state(token, max_age_seconds=-1)


def test_state_cookie_tamper_raises_invalid():
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-4 (extension) — flipping signature bytes raises StateInvalid.
    """
    from transcription_api.auth.state_cookie import StateInvalid, sign_state, verify_state

    token = sign_state({"state": "xyz", "code_verifier": "v" * 64})
    tampered = token[:-3] + "AAA"  # mutilate the trailing signature
    with pytest.raises(StateInvalid):
        verify_state(tampered)
