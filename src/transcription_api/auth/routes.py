"""Auth routes — FastAPI APIRouter.

Spec: SPEC-capa2-auth-msentra-v1
Routes implemented progressively:
- T4 (Batch 1): router + /auth/_ping placeholder for AC-1 wire test.
- T5/T6 (Batch 2): GET /auth/login.
- T7-T9 (Batch 3): GET /auth/callback (happy paths).
- T10-T12 (Batch 4): callback error handling.
- T13-T17 (Batch 5): GET /auth/me, POST /auth/regenerate-mcp-token, POST /auth/logout.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/_ping")
async def ping() -> dict:
    """Smoke endpoint to confirm the router is wired into the app (T4)."""
    return {"ok": True}
