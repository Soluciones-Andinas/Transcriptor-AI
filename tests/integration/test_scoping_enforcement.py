"""Per-user query scoping enforcement.

Spec: SPEC-capa1-postgres-orm-v1, review fix S-1.
Capa 2 review CR-5: listener is now FAIL-CLOSED — a per-user query on a
session that has neither `user_id` armed nor `scoping_bypass=True` raises
`ScopingNotArmedError` instead of silently returning all rows.

Module under test: src/transcription_api/db/scoping.py

These tests prove the load-bearing invariant for Capa 6: even if a query
forgets the `WHERE user_id = X` clause, the scoping listener injects it
when `session.info["user_id"]` is set. Without this enforcement,
"developer discipline" guarantees an inter-tenant data leak the moment
two users share an MCP query path.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.factories import (
    make_bearer,
    make_image,
    make_oauth_token,
    make_transcription,
    make_upload_session,
    make_user,
)
from transcription_api.db import enable_per_user_scoping, set_session_user
from transcription_api.db.models import (
    Image,
    McpBearer,
    OAuthToken,
    Transcription,
    UploadSession,
)
from transcription_api.db.scoping import ScopingNotArmedError, bypass_scoping

pytestmark = pytest.mark.requires_docker


@pytest.fixture(autouse=True, scope="module")
def _ensure_scoping_listener_installed():
    """Install the scoping listener (idempotent) before any test runs."""
    enable_per_user_scoping()


@pytest.fixture(autouse=True)
def _own_bypass(session):
    """These tests verify the listener — opt out of the conftest's blanket
    `scoping_bypass=True` so we can observe the fail-closed behavior."""
    session.info.pop("scoping_bypass", None)
    yield


@pytest.mark.parametrize(
    "model,factory_kwargs",
    [
        (OAuthToken, {}),
        (McpBearer, {}),
        (Transcription, {}),
        (Image, {}),
        (UploadSession, {}),
    ],
    ids=["OAuthToken", "McpBearer", "Transcription", "Image", "UploadSession"],
)
async def test_scoped_query_returns_only_active_user_rows(session, model, factory_kwargs):
    """
    Spec: SPEC-capa1-postgres-orm-v1, S-1.
    Capa 2 review: CR-5 (fail-closed).

    For every per-user model, when `session.info["user_id"]` is set, a
    naive `select(Model)` returns only that user's rows — never the other
    user's, no matter how the query is phrased. With neither `user_id`
    nor `scoping_bypass` set, the same query raises `ScopingNotArmedError`.
    """
    # Factories use INSERT (not intercepted by the listener), so they
    # succeed even without bypass. We do however need to insert under
    # bypass so any internal SELECT-on-flush doesn't trip fail-closed.
    with bypass_scoping(session):
        alice = await make_user(session, email="alice@sandinas.test")
        bob = await make_user(session, email="bob@sandinas.test")
        bearer_alice = await make_bearer(session, user_id=alice.id)
        bearer_bob = await make_bearer(session, user_id=bob.id)
        tr_alice = await make_transcription(session, user_id=alice.id)
        tr_bob = await make_transcription(session, user_id=bob.id)

        if model is OAuthToken:
            await make_oauth_token(session, user_id=alice.id)
            await make_oauth_token(session, user_id=bob.id)
        elif model is Image:
            await make_image(session, transcription_id=tr_alice.id, user_id=alice.id)
            await make_image(session, transcription_id=tr_bob.id, user_id=bob.id)
        elif model is UploadSession:
            await make_upload_session(session, user_id=alice.id, bearer_id=bearer_alice.id)
            await make_upload_session(session, user_id=bob.id, bearer_id=bearer_bob.id)

    # Without scoping AND without bypass: must RAISE (CR-5 fail-closed).
    set_session_user(session, None)
    with pytest.raises(ScopingNotArmedError):
        await session.execute(select(model))

    # Scope to alice — must see ONLY her rows.
    set_session_user(session, alice.id)
    scoped_alice = (await session.execute(select(model))).scalars().all()
    assert len(scoped_alice) >= 1
    assert all(row.user_id == alice.id for row in scoped_alice), (
        f"scoping leaked: {model.__name__} returned rows from another user under alice's session"
    )

    # Scope to bob — must see ONLY his rows. No alice rows leak.
    set_session_user(session, bob.id)
    scoped_bob = (await session.execute(select(model))).scalars().all()
    assert len(scoped_bob) >= 1
    assert all(row.user_id == bob.id for row in scoped_bob), (
        f"scoping leaked: {model.__name__} returned rows from another user under bob's session"
    )


async def test_unscoped_session_raises_fail_closed(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1, S-1.
    Capa 2 review: CR-5.

    A per-user query on a session with neither `user_id` nor
    `scoping_bypass` set must raise `ScopingNotArmedError`. The OLD
    contract (silently return all rows) was a silent-leak hazard: a
    forgotten dependency in any future endpoint exposed every user's data.
    """
    with bypass_scoping(session):
        alice = await make_user(session)
        bob = await make_user(session)
        await make_transcription(session, user_id=alice.id)
        await make_transcription(session, user_id=bob.id)

    assert "user_id" not in session.info
    assert "scoping_bypass" not in session.info  # popped by autouse fixture

    with pytest.raises(ScopingNotArmedError):
        await session.execute(select(Transcription))


async def test_bypass_admin_pattern_sees_all_rows(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1, S-1.
    Capa 2 review: S-5.

    Legitimate cross-user maintenance (cleanup jobs, admin reports, auth
    lookups) wraps the query in `bypass_scoping(session)`. Inside the
    block, queries return all rows regardless of user_id.
    """
    with bypass_scoping(session):
        alice = await make_user(session)
        bob = await make_user(session)
        await make_transcription(session, user_id=alice.id)
        await make_transcription(session, user_id=bob.id)

        rows = (await session.execute(select(Transcription))).scalars().all()
        user_ids = {r.user_id for r in rows}
        assert {alice.id, bob.id}.issubset(user_ids), (
            f"bypass_scoping must let queries see both users; got user_ids={user_ids}"
        )


async def test_scoping_does_not_filter_users_table(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1, S-1.

    `users` does NOT carry a user_id column (the user_id IS the user.id).
    Scoping must NOT filter user lookups, otherwise authentication / lookup
    flows break entirely. This is asserted explicitly here because the
    fail-closed change (CR-5) relies on `_scoped_models()` correctly
    excluding `users`.
    """
    from transcription_api.db.models import User

    with bypass_scoping(session):
        alice = await make_user(session)
        await make_user(session)  # second user
    set_session_user(session, alice.id)

    # Scoped session must still find alice and any other user (the listener
    # leaves users alone).
    rows = (await session.execute(select(User))).scalars().all()
    assert len(rows) >= 2, (
        f"scoping listener must not filter the users table; got {len(rows)} rows"
    )


async def test_scoping_bypass_flag(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1, S-1.
    Capa 2 review: S-5 (context manager wraps the same flag).

    `session.info["scoping_bypass"] = True` (or `bypass_scoping(session)`)
    must override scoping even when a `user_id` is armed.
    """
    with bypass_scoping(session):
        alice = await make_user(session)
        bob = await make_user(session)
        await make_transcription(session, user_id=alice.id)
        await make_transcription(session, user_id=bob.id)

    set_session_user(session, alice.id)
    with bypass_scoping(session):
        rows = (await session.execute(select(Transcription))).scalars().all()
        user_ids = {r.user_id for r in rows}
        assert {alice.id, bob.id}.issubset(user_ids), (
            "scoping_bypass=True must let queries see all users"
        )
