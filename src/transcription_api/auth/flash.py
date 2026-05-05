"""Flash cookie helper for the MCP bearer plaintext.

Spec: SPEC-capa2-auth-msentra-v1
RF-AUTH-04 special case: the bearer plaintext is shown to the user exactly
once after first login. We pass it from the `/auth/callback` response to the
UI's `/mcp-setup` page via a short-lived `mcp_bearer_flash` cookie:

- HttpOnly + Secure + SameSite=Strict (browser-only, same-site only).
- TTL 60 s — long enough for the redirect chain, short enough that a copy
  of the cookie left somewhere becomes useless quickly.
- The cookie is consumed by `GET /auth/me` (B5/T13), which reads it,
  includes the plaintext in its JSON response, and immediately sets a
  Max-Age=0 Set-Cookie to clear it.

Capa 2 review H-8: `path`, `secure`, and `samesite` are explicit on BOTH
the set and the delete. Browsers match cookies for deletion on
(name, domain, path, secure) — if any attribute drifts between set and
delete, the browser silently keeps the cookie around and the "shown once"
guarantee is broken on the next visit to /auth/me.
"""
from __future__ import annotations

from fastapi import Request, Response

COOKIE_NAME = "mcp_bearer_flash"
TTL_SECONDS = 60
COOKIE_PATH = "/"  # match default; explicit so set/delete align (H-8)


def set_bearer_flash(response: Response, plaintext: str) -> None:
    """Attach the bearer plaintext to the response as a short-lived cookie."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=plaintext,
        max_age=TTL_SECONDS,
        path=COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="strict",
    )


def pop_bearer_flash(request: Request, response: Response) -> str | None:
    """Read the flash cookie and queue its deletion on the response.

    Returns the plaintext if present, else None. Always sets a Max-Age=0
    Set-Cookie matching the original (name, path, secure, samesite) so the
    next request won't see it (cookie is consumed once).
    """
    plaintext = request.cookies.get(COOKIE_NAME)
    if plaintext:
        response.delete_cookie(
            key=COOKIE_NAME,
            path=COOKIE_PATH,
            secure=True,
            httponly=True,
            samesite="strict",
        )
    return plaintext
