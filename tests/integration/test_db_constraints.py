"""Constraint + Spanish FTS behavioral tests.

Spec: SPEC-capa1-postgres-orm-v1
Plan: docs/sesiones/2026-04-30-capa1-postgres-orm-plan.md
Covers:
- AC-13 — `uq_mcp_bearers_active_per_user` (partial UNIQUE WHERE revoked_at IS NULL)
  enforces "at most one active bearer per user" even with different token_hash.
- AC-14 — GIN tsvector spanish index supports `plainto_tsquery` matching.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from tests.factories import make_bearer, make_transcription, make_user
from transcription_api.db.models import Transcription

pytestmark = pytest.mark.requires_docker


# ---------------------------------------------------------------------------
# AC-13 — partial UNIQUE on active bearers
# ---------------------------------------------------------------------------
async def test_partial_unique_blocks_two_active_bearers(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-13 — inserting a second bearer with revoked_at IS NULL for the
    same user must raise IntegrityError. The two bearers carry DIFFERENT
    token_hash values to isolate the partial-UNIQUE invariant from the global
    UNIQUE on token_hash (RF-AUTH-07).
    """
    user = await make_user(session)
    await make_bearer(session, user_id=user.id, token_hash="hash-A", revoked_at=None)

    with pytest.raises(IntegrityError):
        await make_bearer(session, user_id=user.id, token_hash="hash-B", revoked_at=None)


async def test_partial_unique_allows_active_after_revoking(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-13 — a revoked bearer (revoked_at NOT NULL) does NOT block a
    new active one for the same user. This is the rotate-bearer flow:
    revoke old, issue new.
    """
    from datetime import datetime, timezone
    user = await make_user(session)
    await make_bearer(
        session,
        user_id=user.id,
        token_hash="old-hash",
        revoked_at=datetime.now(tz=timezone.utc),
    )
    # Should NOT raise: the prior bearer is revoked, so the partial UNIQUE
    # predicate (revoked_at IS NULL) does not include it.
    await make_bearer(session, user_id=user.id, token_hash="new-hash", revoked_at=None)


async def test_partial_unique_under_concurrent_inserts(migrated_db_url):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-13 (review fix M-6) — under concurrent inserts of two
    active bearers for the same user, the partial UNIQUE must let exactly
    one succeed and the other raise IntegrityError. This catches a future
    regression where someone replaces the index with an app-level check
    that has a race window.
    """
    import asyncio
    import secrets

    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine(migrated_db_url, future=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    # Setup: one user, committed so both concurrent sessions can see them.
    async with factory() as setup:
        user = await make_user(setup)
        await setup.commit()
        user_id = user.id

    async def _insert_active_bearer() -> bool:
        """Returns True on commit, False on IntegrityError."""
        async with factory() as s:
            try:
                await make_bearer(
                    s, user_id=user_id, token_hash=secrets.token_hex(16), revoked_at=None,
                )
                await s.commit()
                return True
            except IntegrityError:
                return False

    try:
        results = await asyncio.gather(
            _insert_active_bearer(),
            _insert_active_bearer(),
            return_exceptions=False,
        )
        # Exactly one must succeed.
        assert results.count(True) == 1, (
            f"partial UNIQUE failed under race — both inserts succeeded? results={results}"
        )
        assert results.count(False) == 1
    finally:
        # Cleanup: remove the user we created so the test is idempotent.
        async with factory() as s:
            from sqlalchemy import delete

            from transcription_api.db.models import User
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()
        await engine.dispose()


# ---------------------------------------------------------------------------
# AC-14 — Spanish full-text search on transcriptions.text
# ---------------------------------------------------------------------------
async def test_spanish_fts_matches_inserted_text(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-14 — to_tsvector('spanish', text) @@ plainto_tsquery('spanish', q)
    matches transcripts containing the query term.
    """
    user = await make_user(session)
    await make_transcription(
        session,
        user_id=user.id,
        text="Discutimos arquitectura de microservicios y migraciones de base de datos.",
    )

    rows = (
        await session.execute(
            select(Transcription).where(
                func.to_tsvector("spanish", Transcription.text_content).op("@@")(
                    func.plainto_tsquery("spanish", "arquitectura")
                )
            )
        )
    ).scalars().all()

    assert len(rows) == 1
    assert "arquitectura" in rows[0].text_content


async def test_spanish_fts_stems_inflected_forms(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-14 — the spanish stemmer reduces inflected forms; searching
    for the lemma matches plural / conjugated forms in the indexed text.
    """
    user = await make_user(session)
    # Inserted text uses a plural; the query uses the singular lemma.
    await make_transcription(
        session,
        user_id=user.id,
        text="El equipo decidió priorizar las migraciones para esta semana.",
    )

    rows = (
        await session.execute(
            select(Transcription).where(
                func.to_tsvector("spanish", Transcription.text_content).op("@@")(
                    func.plainto_tsquery("spanish", "migración")
                )
            )
        )
    ).scalars().all()

    assert len(rows) == 1, (
        "spanish stemmer should match 'migración' against indexed 'migraciones'"
    )


async def test_spanish_fts_does_not_match_unrelated_words(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-14 — sanity check: queries that don't appear in the text return zero rows.
    """
    user = await make_user(session)
    await make_transcription(
        session,
        user_id=user.id,
        text="Hablamos sobre el plan de capacitación del equipo.",
    )

    rows = (
        await session.execute(
            select(Transcription).where(
                func.to_tsvector("spanish", Transcription.text_content).op("@@")(
                    func.plainto_tsquery("spanish", "kubernetes")
                )
            )
        )
    ).scalars().all()

    assert rows == []
