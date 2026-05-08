"""``_resolve_user_id`` rejects non-UUID values armed in ``session.info``.

Spec: SPEC-capa4-mcp-v1
Covers (G8 — review-fixes Tier 2):
- A miswired call site that armed ``session.info["user_id"]`` with a
  string (e.g., the un-UUID-cast bearer payload) would silently pass
  through the listener as a bind parameter, making cross-user queries
  non-deterministic. The type guard surfaces the misconfiguration as
  ``ScopingNotArmedError`` instead.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest


def _state(info: dict) -> MagicMock:
    state = MagicMock()
    state.session.info = info
    return state


def test_resolve_user_id_returns_uuid_when_armed_correctly() -> None:
    from transcription_api.db.scoping import _resolve_user_id

    uid = uuid.uuid4()
    assert _resolve_user_id(_state({"user_id": uid})) == uid


def test_resolve_user_id_returns_none_when_unarmed() -> None:
    from transcription_api.db.scoping import _resolve_user_id

    assert _resolve_user_id(_state({})) is None


def test_resolve_user_id_raises_on_non_uuid_string() -> None:
    from transcription_api.db.scoping import (
        ScopingNotArmedError,
        _resolve_user_id,
    )

    with pytest.raises(ScopingNotArmedError, match="expected uuid.UUID"):
        _resolve_user_id(_state({"user_id": "not-a-uuid"}))


def test_resolve_user_id_raises_on_int() -> None:
    from transcription_api.db.scoping import (
        ScopingNotArmedError,
        _resolve_user_id,
    )

    with pytest.raises(ScopingNotArmedError, match="expected uuid.UUID"):
        _resolve_user_id(_state({"user_id": 42}))
