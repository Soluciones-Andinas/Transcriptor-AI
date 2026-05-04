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

    Review fix M-1: insert ≥3 rows per child for the target user, plus a
    second untouched user with their own data. After delete:
    - all rows for the target user must be gone (catches partial cascades);
    - all rows for the untouched user must remain (catches over-cascade
      bugs, e.g. a missing WHERE clause in a future DELETE-trigger).

    Postgres evaluates the cascade tree within the same DELETE statement, so
    even though `upload_sessions.bearer_id` has no cascade, the rows go away
    via their `user_id` cascade before the bearer FK is checked.
    """
    target = await make_user(session, email="target@sandinas.test")
    bystander = await make_user(session, email="bystander@sandinas.test")

    # Multi-row setup for the user being deleted.
    target_bearer1 = await make_bearer(session, user_id=target.id)
    target_bearer2 = await make_bearer(
        session, user_id=target.id,
        revoked_at=__import__("datetime").datetime.now(
            tz=__import__("datetime").timezone.utc,
        ),
    )
    await make_oauth_token(session, user_id=target.id)
    target_tr1 = await make_transcription(session, user_id=target.id)
    target_tr2 = await make_transcription(session, user_id=target.id)
    target_tr3 = await make_transcription(session, user_id=target.id)
    await make_image(session, transcription_id=target_tr1.id, user_id=target.id)
    await make_image(session, transcription_id=target_tr2.id, user_id=target.id)
    await make_image(session, transcription_id=target_tr3.id, user_id=target.id)
    await make_upload_session(session, user_id=target.id, bearer_id=target_bearer1.id)
    await make_upload_session(session, user_id=target.id, bearer_id=target_bearer1.id)
    await make_upload_session(session, user_id=target.id, bearer_id=target_bearer2.id)

    # Untouched bystander rows.
    bystander_bearer = await make_bearer(session, user_id=bystander.id)
    await make_oauth_token(session, user_id=bystander.id)
    bystander_tr = await make_transcription(session, user_id=bystander.id)
    await make_image(session, transcription_id=bystander_tr.id, user_id=bystander.id)
    await make_upload_session(session, user_id=bystander.id, bearer_id=bystander_bearer.id)

    expected_target_counts = {
        OAuthToken: 1,
        McpBearer: 2,
        Transcription: 3,
        Image: 3,
        UploadSession: 3,
    }
    for model, expected in expected_target_counts.items():
        n = (
            await session.execute(
                select(func.count()).select_from(model).where(model.user_id == target.id)
            )
        ).scalar_one()
        assert n == expected, f"setup invariant broken on {model.__name__}: {n} != {expected}"

    # Cascade delete the target.
    await session.execute(delete(User).where(User.id == target.id))
    await session.flush()

    for model in expected_target_counts:
        target_n = (
            await session.execute(
                select(func.count()).select_from(model).where(model.user_id == target.id)
            )
        ).scalar_one()
        bystander_n = (
            await session.execute(
                select(func.count()).select_from(model).where(model.user_id == bystander.id)
            )
        ).scalar_one()
        assert target_n == 0, f"cascade incomplete: {model.__name__} still has {target_n} target rows"
        assert bystander_n == 1, (
            f"cascade over-reached: {model.__name__} bystander rows changed "
            f"({bystander_n} != 1)"
        )
