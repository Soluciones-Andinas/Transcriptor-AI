"""Per-user query scoping enforcement.

Spec: SPEC-capa1-postgres-orm-v1, review fix S-1.
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

from transcription_api.db import enable_per_user_scoping, set_session_user
from transcription_api.db.models import (
    Image,
    McpBearer,
    OAuthToken,
    Transcription,
    UploadSession,
)
from tests.factories import (
    make_bearer,
    make_image,
    make_oauth_token,
    make_transcription,
    make_upload_session,
    make_user,
)


pytestmark = pytest.mark.requires_docker


@pytest.fixture(autouse=True, scope="module")
def _ensure_scoping_listener_installed():
    """Install the scoping listener (idempotent) before any test runs."""
    enable_per_user_scoping()


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

    For every per-user model, when `session.info["user_id"]` is set, a
    naive `select(Model)` returns only that user's rows — never the other
    user's, no matter how the query is phrased.
    """
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
    # bearers and transcriptions already inserted above

    # Without scoping: see both rows.
    set_session_user(session, None)
    unscoped = (await session.execute(select(model))).scalars().all()
    assert len(unscoped) >= 2

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


async def test_unscoped_session_sees_all_rows(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1, S-1.

    If `session.info["user_id"]` is not set (admin / migration context),
    the listener is a no-op — queries return all rows regardless of
    user_id. This is critical for cleanup jobs and Capa 1 tests.
    """
    alice = await make_user(session)
    bob = await make_user(session)
    await make_transcription(session, user_id=alice.id)
    await make_transcription(session, user_id=bob.id)

    # Default state: no user_id in session.info.
    assert "user_id" not in session.info
    rows = (await session.execute(select(Transcription))).scalars().all()
    user_ids = {r.user_id for r in rows}
    assert {alice.id, bob.id}.issubset(user_ids), (
        f"unscoped session must see both users' rows; got user_ids={user_ids}"
    )


async def test_scoping_does_not_filter_users_table(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1, S-1.

    `users` does NOT carry a user_id column (the user_id IS the user.id).
    Scoping must NOT filter user lookups, otherwise authentication / lookup
    flows break entirely.
    """
    from transcription_api.db.models import User

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

    Legitimate cross-user maintenance (cleanup jobs, admin reports) needs
    to bypass scoping even when a user_id is set. The escape hatch is
    `session.info["scoping_bypass"] = True`.
    """
    alice = await make_user(session)
    bob = await make_user(session)
    await make_transcription(session, user_id=alice.id)
    await make_transcription(session, user_id=bob.id)

    set_session_user(session, alice.id)
    session.info["scoping_bypass"] = True
    try:
        rows = (await session.execute(select(Transcription))).scalars().all()
        user_ids = {r.user_id for r in rows}
        assert {alice.id, bob.id}.issubset(user_ids), (
            "scoping_bypass=True must let queries see all users"
        )
    finally:
        del session.info["scoping_bypass"]
