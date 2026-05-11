"""Auth routes — FastAPI APIRouter.

Spec: SPEC-capa2-auth-msentra-v1
Routes implemented progressively:
- T5 (Batch 2): GET /auth/login — RF-AUTH-01 (initiate OAuth + PKCE).
- T6 (Batch 2): T5 also handles already-authenticated case (RF-AUTH-01 special case).
- T7-T9 (Batch 3): GET /auth/callback (happy paths).
- T10-T12 (Batch 4): callback error handling.
- T13-T17 (Batch 5): GET /auth/me, POST /auth/regenerate-mcp-token, POST /auth/logout.

Capa 2 review fixes applied here:
- CR-1: JWKS rotation retry — on `IdTokenInvalid` we force-refresh JWKS once
  and re-validate, before failing.
- CR-3: KeyError guards around the MS /token JSON: id_token / access_token /
  refresh_token may all be missing on a malformed-but-2xx response.
- M-1, M-2: collapsed `__import__("datetime")` and inline imports into
  module-level imports.
- M-3: `mcp_url` is built from `settings.public_base_url`.
- M-6: `/auth/me` builds the response in one pass — no rebuild trick.
- M-8: `/auth/login` logs (not silently swallows) the SessionInvalid case.
- H-9: missing required claim (`email`/`preferred_username`) is mapped to
  AUTH_PROVIDER_UNAVAILABLE (drift logged) instead of yielding "" silently.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from ..config import settings
from ..db import get_session
from ..db.models import McpBearer, OAuthToken, User
from ..db.scoping import bypass_scoping
from .crypto import encrypt_token
from .dependencies import get_current_user_web
from .flash import pop_bearer_flash, set_bearer_flash
from .mcp_bearer import generate_bearer
from .oauth_client import (
    IdTokenInvalid,
    OAuthCodeInvalid,
    OAuthProviderUnavailable,
    TenantNotAllowed,
    build_authorize_url,
    exchange_code,
    fetch_jwks,
    generate_pkce,
    validate_id_token,
)
from .session import SessionInvalid, create_session_token, decode_session_token
from .state_cookie import StateInvalid, sign_state, verify_state

logger = logging.getLogger("transcription_api.auth")


def _mcp_url() -> str:
    """Public URL of the MCP endpoint, derived from settings (M-3)."""
    return f"{settings.public_base_url.rstrip('/')}/mcp"

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
        except SessionInvalid as exc:
            # Cookie present but invalid/expired — proceed with fresh login.
            # M-8: log so operators can spot misconfigured tokens or expired
            # sessions during incident triage; the user-facing flow is the
            # same as a missing cookie.
            logger.info("auth_login_session_invalid reason=%s", exc.__class__.__name__)

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
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",  # Lax — needed because the OAuth provider redirects back
    )
    # D-059 / wiki/05 §7: contractual structured log for RF-AUTH-01 step 5.
    # ``request_id`` is a fresh UUID per login flow start; correlates with
    # ``auth_callback_received`` when the same browser completes the round-trip.
    logger.info("auth_login_started request_id=%s", uuid.uuid4())
    return response


@router.get("/callback")
async def callback(
    request: Request,
    code: str,
    state: str,
    db: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Receive the authorization code from MS Entra and finalize login.

    Implements RF-AUTH-02 (callback validation + user upsert + cookie session)
    + RF-AUTH-04 (emit MCP bearer at first login). Error paths in T10-T12
    extend this with mapping to AUTH_INVALID_STATE / AUTH_TENANT_NOT_ALLOWED /
    AUTH_PROVIDER_UNAVAILABLE.
    """
    # D-059 / wiki/05 §7: contractual structured log for RF-AUTH-02 step 1.
    # Emitted before state validation so failure paths still trace the
    # callback hit; the success flag is finalized at the end of the handler.
    request_id = uuid.uuid4()
    logger.info("auth_callback_received request_id=%s", request_id)

    # 1. Validate cookie state (RF-AUTH-02 step 1).
    state_cookie = request.cookies.get("oauth_state")
    if not state_cookie:
        # Cookie missing — user pegó URL directo, AUTH_INVALID_STATE in B4.
        return RedirectResponse(url="/login?error=AUTH_INVALID_STATE", status_code=302)
    try:
        state_payload = verify_state(state_cookie)
    except StateInvalid:
        return RedirectResponse(url="/login?error=AUTH_INVALID_STATE", status_code=302)
    if state_payload.get("state") != state:
        return RedirectResponse(url="/login?error=AUTH_INVALID_STATE", status_code=302)

    code_verifier = state_payload["code_verifier"]

    # 2. Exchange code for tokens (RF-AUTH-02 step 3).
    # RF-AUTH-05: 5xx / network failure → AUTH_PROVIDER_UNAVAILABLE.
    # RF-AUTH-02 ERR: 4xx (invalid_grant, expired code) → AUTH_INVALID_OAUTH_CODE.
    try:
        token_response = await exchange_code(code=code, code_verifier=code_verifier)
    except OAuthProviderUnavailable:
        logger.warning("auth_provider_unavailable error_id=AUTH_PROVIDER_UNAVAILABLE")
        return RedirectResponse(
            url="/login?error=AUTH_PROVIDER_UNAVAILABLE", status_code=302,
        )
    except OAuthCodeInvalid:
        logger.warning("auth_invalid_oauth_code error_id=AUTH_INVALID_OAUTH_CODE")
        return RedirectResponse(
            url="/login?error=AUTH_INVALID_OAUTH_CODE", status_code=302,
        )

    # CR-3: defensive — MS contract says these MUST be present on a 200,
    # but a malformed-but-2xx response is treated like a provider failure
    # rather than crashing with KeyError 500.
    try:
        id_token = token_response["id_token"]
        access_token = token_response["access_token"]
        refresh_token = token_response["refresh_token"]
    except KeyError as exc:
        logger.warning(
            "auth_provider_unavailable error_id=AUTH_PROVIDER_UNAVAILABLE "
            "source=token_response_missing_field field=%s",
            exc.args[0] if exc.args else "unknown",
        )
        return RedirectResponse(
            url="/login?error=AUTH_PROVIDER_UNAVAILABLE", status_code=302,
        )

    # 3. Validate id_token (RF-AUTH-02 step 5 / RF-AUTH-03).
    # RF-AUTH-03: tid mismatch → AUTH_TENANT_NOT_ALLOWED (regression of Privacy if missed).
    # CR-1: on `IdTokenInvalid` (e.g. signature failure due to MS rotating its
    # signing key while our JWKS is cached), we force-refresh JWKS once and
    # re-validate before giving up.
    try:
        jwks = await fetch_jwks()
        claims = validate_id_token(id_token, jwks)
    except TenantNotAllowed:
        logger.warning("auth_tenant_not_allowed error_id=AUTH_TENANT_NOT_ALLOWED")
        return RedirectResponse(
            url="/login?error=AUTH_TENANT_NOT_ALLOWED", status_code=302,
        )
    except IdTokenInvalid:
        logger.info(
            "auth_jwks_retry_on_signature_failure — forcing JWKS refresh and retrying once",
        )
        try:
            jwks = await fetch_jwks(force_refresh=True)
            claims = validate_id_token(id_token, jwks)
        except TenantNotAllowed:
            logger.warning("auth_tenant_not_allowed error_id=AUTH_TENANT_NOT_ALLOWED")
            return RedirectResponse(
                url="/login?error=AUTH_TENANT_NOT_ALLOWED", status_code=302,
            )
        except IdTokenInvalid as exc:
            logger.warning(
                "auth_id_token_invalid_after_retry error_id=AUTH_PROVIDER_UNAVAILABLE reason=%s",
                exc,
            )
            return RedirectResponse(
                url="/login?error=AUTH_PROVIDER_UNAVAILABLE", status_code=302,
            )
        except OAuthProviderUnavailable:
            logger.warning(
                "auth_provider_unavailable error_id=AUTH_PROVIDER_UNAVAILABLE source=jwks_retry",
            )
            return RedirectResponse(
                url="/login?error=AUTH_PROVIDER_UNAVAILABLE", status_code=302,
            )
    except OAuthProviderUnavailable:
        logger.warning(
            "auth_provider_unavailable error_id=AUTH_PROVIDER_UNAVAILABLE source=jwks",
        )
        return RedirectResponse(
            url="/login?error=AUTH_PROVIDER_UNAVAILABLE", status_code=302,
        )

    # 4. Extract identity (RF-AUTH-02 step 6).
    try:
        ms_oid = uuid.UUID(claims["oid"])
    except (KeyError, ValueError) as exc:
        logger.warning(
            "auth_id_token_missing_oid error_id=AUTH_PROVIDER_UNAVAILABLE reason=%s",
            exc.__class__.__name__,
        )
        return RedirectResponse(
            url="/login?error=AUTH_PROVIDER_UNAVAILABLE", status_code=302,
        )
    # H-9: a successfully signed id_token without any usable user identifier
    # is a bizarre MS contract violation — fail closed instead of inserting
    # a User row with email = "" (which would later collide with the next
    # user lacking an email and break uniqueness assumptions).
    email = claims.get("email") or claims.get("preferred_username")
    if not email:
        logger.warning(
            "auth_id_token_missing_email error_id=AUTH_PROVIDER_UNAVAILABLE oid=%s",
            ms_oid,
        )
        return RedirectResponse(
            url="/login?error=AUTH_PROVIDER_UNAVAILABLE", status_code=302,
        )
    display_name = claims.get("name") or email or str(ms_oid)

    # 5. Upsert user (RF-AUTH-02 step 7-8).
    # The whole upsert runs under `bypass_scoping`: at this point the user is
    # being established (or refreshed) so the per-user scoping listener
    # (CR-5 fail-closed) cannot enforce — there is no armed user yet.
    # Once the cookie session is issued, downstream requests go through
    # `get_current_user_web` which arms scoping the proper way.
    with bypass_scoping(db):
        existing = (
            await db.execute(select(User).where(User.microsoft_oid == ms_oid))
        ).scalar_one_or_none()

        access_expires_at = datetime.now(tz=timezone.utc) + timedelta(
            seconds=int(token_response.get("expires_in", 3600)),
        )

        if existing is None:
            # First login: create user + tokens + bearer.
            user = User(
                id=uuid.uuid4(),
                microsoft_oid=ms_oid,
                email=email,
                display_name=display_name,
            )
            db.add(user)
            await db.flush()

            oauth_tok = OAuthToken(
                id=uuid.uuid4(),
                user_id=user.id,
                ms_access_token_encrypted=encrypt_token(access_token),
                ms_refresh_token_encrypted=encrypt_token(refresh_token),
                ms_access_expires_at=access_expires_at,
            )
            db.add(oauth_tok)

            bearer_plaintext, bearer_hash = generate_bearer()
            bearer = McpBearer(
                id=uuid.uuid4(),
                user_id=user.id,
                token_hash=bearer_hash,
                name="initial",
            )
            db.add(bearer)
            await db.commit()

            logger.info("auth_user_created user_id=%s", user.id)
            # D-059 / wiki/05 §7: contractual log for RF-AUTH-04 first-login
            # bearer emission. Same event name on the regenerate path so a
            # SIEM ingest can sum all bearer creations uniformly.
            logger.info(
                "mcp_bearer_generated user_id=%s bearer_id=%s name=initial",
                user.id,
                bearer.id,
            )
            is_first_login = True
        else:
            # Subsequent login: update last_login_at + refresh tokens.
            await db.execute(
                sql_update(User)
                .where(User.id == existing.id)
                .values(last_login_at=func.now())
            )
            await db.execute(
                sql_update(OAuthToken)
                .where(OAuthToken.user_id == existing.id)
                .values(
                    ms_access_token_encrypted=encrypt_token(access_token),
                    ms_refresh_token_encrypted=encrypt_token(refresh_token),
                    ms_access_expires_at=access_expires_at,
                )
            )
            await db.commit()

            user = existing
            bearer_plaintext = None  # no flash on subsequent login
            is_first_login = False
            logger.info("auth_user_login user_id=%s", user.id)

    # 6. Issue session JWT cookie (RF-AUTH-02 step 9).
    session_token = create_session_token(user_id=user.id, ms_oid=user.microsoft_oid, email=user.email)

    # 7. Build response: redirect to /mcp-setup + cookies (RF-AUTH-02 step 11-12).
    response = RedirectResponse(url="/mcp-setup", status_code=302)
    response.set_cookie(
        key="session",
        value=session_token,
        max_age=86400,
        path="/",
        httponly=True,
        secure=True,
        samesite="strict",
    )
    # H-8: delete attributes must match the set attributes (path, secure,
    # samesite) or the browser silently keeps the cookie around.
    response.delete_cookie(
        key="oauth_state", path="/", secure=True, httponly=True, samesite="lax",
    )
    if is_first_login and bearer_plaintext is not None:
        set_bearer_flash(response, bearer_plaintext)

    return response


# --------------------------------------------------------------------------
# B5 — User-facing endpoints after login.
# --------------------------------------------------------------------------
@router.get("/me")
async def me(
    request: Request,
    user: User = Depends(get_current_user_web),
    db: AsyncSession = Depends(get_session),
):
    """Return the authenticated user's profile + active MCP bearer.

    RF-AUTH-06: includes the bearer plaintext if the `mcp_bearer_flash` cookie
    is present (one-shot pass from /auth/callback). Otherwise plaintext is
    None — only the bearer.id is returned (the user already saved the
    plaintext, or has lost it and must regenerate).

    M-6: build the JSON body once (read flash before constructing the
    response) and let `pop_bearer_flash` queue the cookie deletion onto
    the constructed response — no JSONResponse rebuild trick.
    """
    bearer = (
        await db.execute(
            select(McpBearer)
            .where(McpBearer.user_id == user.id)
            .where(McpBearer.revoked_at.is_(None))
            .order_by(McpBearer.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    flash_plaintext = request.cookies.get("mcp_bearer_flash")
    bearer_payload = None
    if bearer is not None:
        bearer_payload = {
            "id": str(bearer.id),
            "plaintext": flash_plaintext if flash_plaintext else None,
            "created_at": bearer.created_at.isoformat() if bearer.created_at else None,
            "name": bearer.name,
        }

    response = JSONResponse(content={
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        },
        "bearer": bearer_payload,
        "mcp_url": _mcp_url(),
    })

    if flash_plaintext:
        # Consume the one-shot cookie. `pop_bearer_flash` queues the
        # deletion Set-Cookie header onto `response`.
        pop_bearer_flash(request, response)

    return response


@router.post("/regenerate-mcp-token")
async def regenerate_mcp_token(
    user: User = Depends(get_current_user_web),
    db: AsyncSession = Depends(get_session),
):
    """Revoke the user's active MCP bearer and emit a new one.

    RF-AUTH-07: emits a new bearer plaintext (shown ONCE in this response).
    ERR-4: the partial UNIQUE constraint `uq_mcp_bearers_active_per_user`
    prevents two concurrent regenerates from both succeeding. On
    IntegrityError we retry once (re-revoke + re-insert) — last-writer-wins
    semantics; the user only ever races against themselves anyway.
    """
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            await db.execute(
                sql_update(McpBearer)
                .where(McpBearer.user_id == user.id)
                .where(McpBearer.revoked_at.is_(None))
                .values(revoked_at=func.clock_timestamp())
            )
            plaintext, token_hash = generate_bearer()
            new_bearer = McpBearer(
                id=uuid.uuid4(),
                user_id=user.id,
                token_hash=token_hash,
                name="regenerated",
            )
            db.add(new_bearer)
            await db.commit()
            await db.refresh(new_bearer)
            # D-059 / wiki/05 §7: contractual logs for RF-AUTH-07 step 7.
            # Emitted pair (revoked → generated) per regenerate operation;
            # SIEM can reconstruct the lifecycle of every bearer.
            logger.info("mcp_bearer_revoked user_id=%s", user.id)
            logger.info(
                "mcp_bearer_generated user_id=%s bearer_id=%s name=regenerated",
                user.id,
                new_bearer.id,
            )
            return {
                "bearer": {
                    "id": str(new_bearer.id),
                    "plaintext": plaintext,
                    "created_at": new_bearer.created_at.isoformat()
                    if new_bearer.created_at
                    else datetime.now(tz=timezone.utc).isoformat(),
                    "name": new_bearer.name,
                }
            }
        except IntegrityError as exc:
            last_error = exc
            await db.rollback()
            logger.warning(
                "mcp_bearer_regenerate_race user_id=%s attempt=%d", user.id, _attempt,
            )
            continue

    logger.error(
        "mcp_bearer_regenerate_failed_after_retry user_id=%s error=%s",
        user.id, last_error,
    )
    raise HTTPException(
        status_code=500,
        detail={"error_code": "INTERNAL_ERROR", "reason": "regenerate failed after retry"},
    )


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Clear the web session cookie. ALT-1: does NOT revoke the MCP bearer.

    The user might be using the bearer from Claude Desktop (no browser);
    revoking on logout would be a destructive surprise. To revoke explicitly,
    the user calls POST /auth/regenerate-mcp-token (RF-AUTH-07).

    H-8: deletion must mirror the set attributes (path/secure/httponly/
    samesite) — otherwise the browser may keep the original cookie and
    the user remains authenticated until natural expiry.
    """
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(
        key="session", path="/", secure=True, httponly=True, samesite="strict",
    )
    logger.info("auth_logout")
    return response
