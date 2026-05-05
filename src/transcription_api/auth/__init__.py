"""Auth module — Microsoft Entra OAuth + auth endpoints + MCP bearer middleware.

Spec: SPEC-capa2-auth-msentra-v1
Public exports for AC-1: router (HTTP), get_current_user_web (cookie session
dependency), get_current_user_mcp (Authorization Bearer dependency that
activates ADR-014 per-user scoping listener).
"""
from .dependencies import get_current_user_mcp, get_current_user_web
from .routes import router

__all__ = ["router", "get_current_user_web", "get_current_user_mcp"]
