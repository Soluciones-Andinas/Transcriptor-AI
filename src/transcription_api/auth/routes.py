"""Auth routes — FastAPI APIRouter.

Spec: SPEC-capa2-auth-msentra-v1
Routes implemented progressively:
- T5 (Batch 2): GET /auth/login — RF-AUTH-01 (initiate OAuth + PKCE).
- T6 (Batch 2): T5 also handles already-authenticated case (RF-AUTH-01 special case).
- T7-T9 (Batch 3): GET /auth/callback (happy paths).
- T10-T12 (Batch 4): callback error handling.
- T13-T17 (Batch 5): GET /auth/me, POST /auth/regenerate-mcp-token, POST /auth/logout.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .oauth_client import build_authorize_url, generate_pkce
from .session import SessionInvalid, decode_session_token
from .state_cookie import sign_state

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """Start the OAuth flow (RF-AUTH-01).

    If the user already has a valid session cookie, skip the flow and
    redirect straight to /mcp-setup (RF-AUTH-01 special case "user ya logueado").
    Otherwise: generate state + PKCE verifier, sign a state cookie, redirect
    to Microsoft Entra's authorize endpoint.
    """
    # T6 / AC-7: short-circuit when already authenticated.
    session_cookie = request.cookies.get("session")
    if session_cookie:
        try:
            decode_session_token(session_cookie)
            return RedirectResponse(url="/mcp-setup", status_code=302)
        except SessionInvalid:
            # Cookie present but invalid/expired — proceed with fresh login.
            pass

    # T5 / AC-6: standard login start.
    state = secrets.token_hex(32)  # 64 hex chars
    verifier, challenge = generate_pkce()
    cookie_value = sign_state({"state": state, "code_verifier": verifier})
    url = build_authorize_url(state=state, code_challenge=challenge)
    response = RedirectResponse(url=url, status_code=302)
    response.set_cookie(
        key="oauth_state",
        value=cookie_value,
        max_age=300,
        httponly=True,
        secure=True,
        samesite="lax",  # Lax — needed because the OAuth provider redirects back
    )
    return response
