"""Per-user query scoping enforcement.

Spec: SPEC-capa1-postgres-orm-v1, review fix S-1.
Capa 2 review: CR-5 (fail-closed) + S-5 (bypass context manager).

Capa 1 declares `user_id` FK on every per-user table (oauth_tokens,
mcp_bearers, transcriptions, images, upload_sessions). Filtering by it
is currently developer discipline — by Capa 6 (MCP server with arbitrary
user-authenticated queries) "developer discipline" is a leak waiting to
happen.

This module installs a SQLAlchemy `do_orm_execute` event listener that,
when the active session has `session.info["user_id"]` set, transparently
adds `WHERE user_id = X` to the statement when the queried entity has a
`user_id` column.

CR-5 — fail-closed:
- A query against a per-user model on a session that has neither
  `session.info["user_id"]` nor `session.info["scoping_bypass"] = True`
  raises `ScopingNotArmedError` instead of returning all rows.
- Rationale: a forgotten auth dependency in any future endpoint silently
  exposed every user's data. Now it throws loudly at the SQL boundary.

Activation:
- `enable_per_user_scoping()` registers the listener once at import time
  (idempotent).
- Capa 2 auth middleware sets `session.info["user_id"] = current_user.id`
  per request via `get_current_user_web` / `get_current_user_mcp`.
- Auth lookups (verify_bearer, user-by-id during cookie session decode)
  wrap their SELECTs in `bypass_scoping(session)` because the user_id
  is not yet known.
- Tests, admin tasks, and migrations enter `bypass_scoping(session)`
  (the conftest `session` fixture sets the flag once).

Implementation note: we use direct `state.statement.where(...)` rather
than `with_loader_criteria(...)`. The latter has a SQLAlchemy 2.x caching
gotcha where the closure-captured user_id gets baked into the prepared
statement's cache key, so all subsequent queries on the same session reuse
the FIRST user_id seen. Direct WHERE injection on the statement object
re-binds correctly per execution.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState

from .base import Base


class ScopingNotArmedError(RuntimeError):
    """Per-user model query attempted on an un-armed session.

    Raised by the `do_orm_execute` listener when a SELECT/UPDATE/DELETE
    targets a model with `user_id` but the session has neither
    `session.info["user_id"]` set (production-style scoping) nor
    `session.info["scoping_bypass"] = True` (auth lookup / admin / test).

    Action when you hit this in production code: the request is missing
    a `Depends(get_current_user_*)` dependency, or you forgot to wrap a
    legitimate cross-user operation in `bypass_scoping(session)`.
    """

_SCOPED_MODELS_CACHE: set[type] | None = None


def _scoped_models() -> set[type]:
    """Set of ORM classes that carry `user_id`. Computed lazily."""
    global _SCOPED_MODELS_CACHE
    if _SCOPED_MODELS_CACHE is None:
        from . import models  # noqa: F401 — register on Base.metadata

        result: set[type] = set()
        for mapper in Base.registry.mappers:
            cls = mapper.class_
            if "user_id" in mapper.columns.keys():
                result.add(cls)
        _SCOPED_MODELS_CACHE = result
    return _SCOPED_MODELS_CACHE


def _scoping_bypass(state: ORMExecuteState) -> bool:
    info = state.session.info if state.session is not None else {}
    if info.get("scoping_bypass") is True:
        return True
    return state.execution_options.get("scoping_bypass") is True


def _resolve_user_id(state: ORMExecuteState) -> uuid.UUID | None:
    info = state.session.info if state.session is not None else {}
    return info.get("user_id")


def _on_orm_execute(state: ORMExecuteState) -> None:
    """Inject `user_id = X` filter when the queried mapper carries `user_id`.

    Fail-closed: if the queried mapper IS a per-user model and the session
    has neither `user_id` armed nor `scoping_bypass` set, raise
    `ScopingNotArmedError`. CR-5.
    """
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if _scoping_bypass(state):
        return

    mapper = state.bind_mapper
    if mapper is None:
        return
    cls = mapper.class_
    if cls not in _scoped_models():
        return  # not a per-user model, nothing to enforce

    user_id = _resolve_user_id(state)
    if user_id is None:
        raise ScopingNotArmedError(
            f"Query against per-user model {cls.__name__} on a session "
            "that has neither session.info['user_id'] (production scoping) "
            "nor session.info['scoping_bypass']=True (auth lookup / admin). "
            "This guards against silent cross-user data leaks. "
            "If this is a legitimate cross-user op, wrap it in "
            "`with bypass_scoping(session): ...`."
        )

    state.statement = state.statement.where(cls.user_id == user_id)


_LISTENER_INSTALLED = False


def enable_per_user_scoping() -> None:
    """Idempotently install the global per-user scoping listener."""
    global _LISTENER_INSTALLED
    if _LISTENER_INSTALLED:
        return
    from sqlalchemy.orm import Session

    event.listen(Session, "do_orm_execute", _on_orm_execute)
    _LISTENER_INSTALLED = True


def set_session_user(session: Any, user_id: uuid.UUID | None) -> None:
    """Convenience: scope `session` to `user_id` (or clear it)."""
    if user_id is None:
        session.info.pop("user_id", None)
    else:
        session.info["user_id"] = user_id


@contextmanager
def bypass_scoping(session: Any) -> Iterator[None]:
    """Disable per-user scoping for the duration of the `with` block.

    Use ONLY for legitimate cross-user operations:
    - Auth lookups: the user is not yet known (verify_bearer, decode session
      cookie -> SELECT user by id).
    - Admin / migration / fixture queries.
    - Test setup and verification queries (the conftest session fixture
      enters bypass once for the whole test).

    Restoration is exception-safe: the prior value of `scoping_bypass`
    (most often unset) is restored even if the awaited body raises. Using
    this manager beats inline `session.info["scoping_bypass"] = True / pop`
    because the call site explicitly declares the cross-user intent and
    the cleanup is guaranteed.
    """
    prior = session.info.get("scoping_bypass")
    session.info["scoping_bypass"] = True
    try:
        yield
    finally:
        if prior is None:
            session.info.pop("scoping_bypass", None)
        else:
            session.info["scoping_bypass"] = prior
