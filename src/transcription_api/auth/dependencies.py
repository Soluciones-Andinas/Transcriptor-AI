"""FastAPI dependencies for authentication.

Spec: SPEC-capa2-auth-msentra-v1
- `get_current_user_web`: parses JWT cookie session (B5).
- `get_current_user_mcp`: validates Authorization Bearer + activates ADR-014
  per-user scoping listener (B6).

Batch 1 wires placeholder callables so `auth/__init__.py` re-exports work and
AC-1 (`test_auth_module_imports`) can pass. Real implementations land in
later batches per the plan.
"""
from __future__ import annotations

from fastapi import HTTPException


async def get_current_user_web():
    """Placeholder — real impl in Batch 5 (T13 / AC-13)."""
    raise HTTPException(status_code=501, detail="get_current_user_web not implemented yet")


async def get_current_user_mcp():
    """Placeholder — real impl in Batch 6 (T18 / AC-17)."""
    raise HTTPException(status_code=501, detail="get_current_user_mcp not implemented yet")
