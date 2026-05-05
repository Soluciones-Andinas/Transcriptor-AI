"""Integration test — auth module exposes router + dependencies.

Spec: SPEC-capa2-auth-msentra-v1
Plan: docs/sesiones/2026-05-05-capa2-auth-plan.md (T1)
Covers: AC-1.
"""
from __future__ import annotations


def test_auth_module_imports():
    """
    Spec: SPEC-capa2-auth-msentra-v1
    Criterion: AC-1 — modulo auth expone router + dependencies.
    """
    from fastapi import APIRouter

    from transcription_api.auth import (
        get_current_user_mcp,
        get_current_user_web,
        router,
    )

    assert isinstance(router, APIRouter)
    assert callable(get_current_user_web)
    assert callable(get_current_user_mcp)
