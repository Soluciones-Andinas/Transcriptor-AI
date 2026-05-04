"""Per-user scoping invariant + cascade delete.

Spec: SPEC-capa1-postgres-orm-v1
Plan: docs/sesiones/2026-04-30-capa1-postgres-orm-plan.md
Covers:
- AC-11 — `select(...).where(user_id == X)` returns ONLY user X's data.
- AC-12 — `DELETE FROM users WHERE id = X` cascades to oauth_tokens,
  mcp_bearers, transcriptions, images, upload_sessions.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, func, select

from transcription_api.db.models import (
    Image,
    McpBearer,
    OAuthToken,
    Transcription,
    UploadSession,
    User,
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


async def test_per_user_scoping_transcriptions(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-11 — filtering by user_id returns only that user's transcriptions.

    Two users A and B, each with one transcription. A query scoped to A.id
    must return exactly A's row (and never B's).
    """
    a = await make_user(session, email="a@sandinas.test")
    b = await make_user(session, email="b@sandinas.test")
    tr_a = await make_transcription(session, user_id=a.id, audio_hash="hash-a", text="reunión A")
    await make_transcription(session, user_id=b.id, audio_hash="hash-b", text="reunión B")

    rows = (
        await session.execute(
            select(Transcription).where(Transcription.user_id == a.id)
        )
    ).scalars().all()

    assert len(rows) == 1
    assert rows[0].id == tr_a.id
    assert rows[0].audio_hash == "hash-a"
    # Crucially, B's data is not visible under A's scope:
    assert all(r.user_id == a.id for r in rows)


async def test_per_user_scoping_images_via_transcription(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-11 — Image carries denormalized user_id for cheap scope queries.

    Confirms a per-user filter on images directly (without joining transcriptions)
    returns only the right user's images.
    """
    a = await make_user(session)
    b = await make_user(session)
    tr_a = await make_transcription(session, user_id=a.id)
    tr_b = await make_transcription(session, user_id=b.id)
    await make_image(session, transcription_id=tr_a.id, user_id=a.id, filename="a.png")
    await make_image(session, transcription_id=tr_b.id, user_id=b.id, filename="b.png")

    rows = (
        await session.execute(select(Image).where(Image.user_id == a.id))
    ).scalars().all()

    assert len(rows) == 1
    assert rows[0].filename == "a.png"


async def test_cascade_delete_user_clears_all_dependents(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-12 — deleting a User cascades to oauth_tokens, mcp_bearers,
    transcriptions, images, and upload_sessions (all 5 child tables).

    Postgres evaluates the cascade tree within the same DELETE statement, so
    even though `upload_sessions.bearer_id` has no cascade, the row goes away
    via its `user_id` cascade before the bearer FK is checked.
    """
    user = await make_user(session)
    bearer = await make_bearer(session, user_id=user.id)
    await make_oauth_token(session, user_id=user.id)
    tr = await make_transcription(session, user_id=user.id)
    await make_image(session, transcription_id=tr.id, user_id=user.id)
    await make_upload_session(session, user_id=user.id, bearer_id=bearer.id)

    # Sanity: each child table has exactly 1 row for this user before delete.
    for model, label in [
        (OAuthToken, "oauth_tokens"),
        (McpBearer, "mcp_bearers"),
        (Transcription, "transcriptions"),
        (Image, "images"),
        (UploadSession, "upload_sessions"),
    ]:
        n = (
            await session.execute(
                select(func.count()).select_from(model).where(model.user_id == user.id)
            )
        ).scalar_one()
        assert n == 1, f"setup invariant broken: {label} has {n} rows, expected 1"

    # Cascade delete the parent.
    await session.execute(delete(User).where(User.id == user.id))
    await session.flush()

    # All 5 child tables must now have 0 rows for this user.
    for model, label in [
        (OAuthToken, "oauth_tokens"),
        (McpBearer, "mcp_bearers"),
        (Transcription, "transcriptions"),
        (Image, "images"),
        (UploadSession, "upload_sessions"),
    ]:
        n = (
            await session.execute(
                select(func.count()).select_from(model).where(model.user_id == user.id)
            )
        ).scalar_one()
        assert n == 0, f"cascade incomplete: {label} still has {n} rows after user delete"
