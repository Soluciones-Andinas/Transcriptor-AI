"""Per-user query scoping enforcement.

Spec: SPEC-capa1-postgres-orm-v1, review fix S-1.

Capa 1 declares `user_id` FK on every per-user table (oauth_tokens,
mcp_bearers, transcriptions, images, upload_sessions). Filtering by it
is currently developer discipline — by Capa 6 (MCP server with arbitrary
user-authenticated queries) "developer discipline" is a leak waiting to
happen.

This module installs a SQLAlchemy `do_orm_execute` event listener that,
when the active session has `session.info["user_id"]` set, transparently
adds `WHERE user_id = X` to the statement when the queried entity has a
`user_id` column.

Activation:
- `enable_per_user_scoping()` registers the listener once at import time
  (idempotent).
- Capa 2 auth middleware will set `session.info["user_id"] = current_user.id`
  per request.
- Tests/admin/migrations leave it unset and see all rows (no filter).
- Bypass: `session.info["scoping_bypass"] = True`.

Implementation note: we use direct `state.statement.where(...)` rather
than `with_loader_criteria(...)`. The latter has a SQLAlchemy 2.x caching
gotcha where the closure-captured user_id gets baked into the prepared
statement's cache key, so all subsequent queries on the same session reuse
the FIRST user_id seen. Direct WHERE injection on the statement object
re-binds correctly per execution.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState

from .base import Base

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
    """Inject `user_id = X` filter when the queried mapper carries `user_id`."""
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if _scoping_bypass(state):
        return
    user_id = _resolve_user_id(state)
    if user_id is None:
        return

    mapper = state.bind_mapper
    if mapper is None:
        return
    cls = mapper.class_
    if cls not in _scoped_models():
        return

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
