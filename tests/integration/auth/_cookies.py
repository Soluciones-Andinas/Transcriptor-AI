"""Set-Cookie parsing helpers for auth tests.

Capa 2 review M-10 / M-9: replaces fragile substring assertions like
`"Max-Age=0" in raw or 'expires=' in raw.lower()` with a single parser
that yields a structured dict so tests can assert on attributes by name.

Why this matters: the "is this cookie being deleted?" question depends on
EITHER `Max-Age=0` OR an `Expires` in the past. Substring checks miss
either form depending on how the framework formats the deletion header,
and they don't catch the path/secure/samesite drift that breaks H-8
(the browser silently keeps the original cookie).
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def parse_set_cookie(raw: str) -> dict[str, str | bool]:
    """Parse a single Set-Cookie header value into {name, value, **attrs}.

    Lowercased attribute names; flag-only attributes like Secure/HttpOnly
    map to True. Unknown attributes are dropped. Designed for assertions,
    not for cookie-jar use.
    """
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    if not parts:
        raise ValueError(f"empty Set-Cookie: {raw!r}")
    name, _, value = parts[0].partition("=")
    out: dict[str, str | bool] = {"name": name.strip(), "value": value.strip()}
    for attr in parts[1:]:
        if "=" in attr:
            k, _, v = attr.partition("=")
            out[k.strip().lower()] = v.strip()
        else:
            out[attr.strip().lower()] = True
    return out


def is_cookie_deletion(parsed: dict[str, str | bool]) -> bool:
    """Return True if the parsed Set-Cookie is a delete (Max-Age=0 or past Expires)."""
    if parsed.get("max-age") == "0":
        return True
    expires = parsed.get("expires")
    if isinstance(expires, str):
        try:
            dt = parsedate_to_datetime(expires)
        except (TypeError, ValueError):
            return False
        if dt is None:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(tz=timezone.utc)
    return False


def find_set_cookie(headers, name: str) -> dict[str, str | bool]:
    """Locate the first Set-Cookie for `name` in `headers`. Raises AssertionError if absent.

    `headers` is `httpx.Response.headers` — supports `get_list("set-cookie")`.
    """
    for raw in headers.get_list("set-cookie"):
        parsed = parse_set_cookie(raw)
        if parsed["name"] == name:
            return parsed
    raise AssertionError(
        f"no Set-Cookie for {name!r}. Got: {list(headers.get_list('set-cookie'))}"
    )
