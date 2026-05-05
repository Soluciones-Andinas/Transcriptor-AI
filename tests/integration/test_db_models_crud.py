"""CRUD round-trip per ORM model.

Spec: SPEC-capa1-postgres-orm-v1
Plan: docs/sesiones/2026-04-30-capa1-postgres-orm-plan.md
Covers: AC-10 (INSERT + SELECT for each of the 6 models).
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
from transcription_api.db.models import (
    Image,
    McpBearer,
    OAuthToken,
    Transcription,
    UploadSession,
    User,
)

pytestmark = pytest.mark.requires_docker


async def test_crud_user(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-10 — User INSERT + SELECT round-trip.
    """
    user = await make_user(session, email="alice@sandinas.test")
    fetched = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
    assert fetched.email == "alice@sandinas.test"
    assert fetched.display_name == "Test User"
    assert fetched.created_at is not None  # server_default fired


async def test_crud_oauth_token(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-10 — OAuthToken INSERT + SELECT round-trip.
    """
    user = await make_user(session)
    tok = await make_oauth_token(session, user_id=user.id, access=b"\x10\x20", refresh=b"\x30\x40")
    fetched = (
        await session.execute(select(OAuthToken).where(OAuthToken.id == tok.id))
    ).scalar_one()
    assert fetched.user_id == user.id
    assert fetched.ms_access_token_encrypted == b"\x10\x20"
    assert fetched.ms_refresh_token_encrypted == b"\x30\x40"


async def test_crud_mcp_bearer(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-10 — McpBearer INSERT + SELECT round-trip.
    """
    user = await make_user(session)
    bearer = await make_bearer(session, user_id=user.id, token_hash="abcd1234", name="laptop")
    fetched = (
        await session.execute(select(McpBearer).where(McpBearer.id == bearer.id))
    ).scalar_one()
    assert fetched.token_hash == "abcd1234"
    assert fetched.name == "laptop"
    assert fetched.revoked_at is None


async def test_crud_transcription(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-10 — Transcription INSERT + SELECT round-trip including JSONB columns.
    """
    user = await make_user(session)
    tr = await make_transcription(session, user_id=user.id, text="hola mundo")
    fetched = (
        await session.execute(select(Transcription).where(Transcription.id == tr.id))
    ).scalar_one()
    assert fetched.text_content == "hola mundo"
    assert fetched.language == "es"
    assert fetched.segments == {"segments": []}
    assert fetched.extra_metadata["model"] == "whisper-large-v3"


async def test_crud_image(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-10 — Image INSERT + SELECT round-trip.
    """
    user = await make_user(session)
    tr = await make_transcription(session, user_id=user.id)
    img = await make_image(session, transcription_id=tr.id, user_id=user.id, filename="ss.png")
    fetched = (
        await session.execute(select(Image).where(Image.id == img.id))
    ).scalar_one()
    assert fetched.filename == "ss.png"
    assert fetched.transcription_id == tr.id
    assert fetched.user_id == user.id


async def test_crud_upload_session(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-10 — UploadSession INSERT + SELECT round-trip.
    """
    user = await make_user(session)
    bearer = await make_bearer(session, user_id=user.id)
    up = await make_upload_session(session, user_id=user.id, bearer_id=bearer.id, kind="image")
    fetched = (
        await session.execute(select(UploadSession).where(UploadSession.id == up.id))
    ).scalar_one()
    assert fetched.kind == "image"
    assert fetched.status == "requested"  # server default
    assert fetched.bearer_id == bearer.id
    assert fetched.expires_at is not None


async def test_transcription_duration_round_trips_as_decimal(session):
    """
    Review fix H-2: Numeric(10,2) must round-trip as Decimal, not float.
    Mixed Decimal/float arithmetic in consumer code raises TypeError, so this
    invariant matters beyond a docstring.
    """
    from decimal import Decimal

    user = await make_user(session)
    tr = await make_transcription(session, user_id=user.id, duration_seconds=123.45)
    fetched = (
        await session.execute(select(Transcription).where(Transcription.id == tr.id))
    ).scalar_one()
    assert isinstance(fetched.duration_seconds, Decimal)
    assert fetched.duration_seconds == Decimal("123.45")


async def test_transcription_jsonb_round_trips_nested(session):
    """
    Review fix (pr-test-analyzer #3): non-empty nested JSONB must round-trip
    losslessly. The empty `{"segments": []}` case in factories never exercises
    nested objects, floats, or unicode in keys/values.
    """
    user = await make_user(session)
    nested_segments = {
        "segments": [
            {
                "start": 0.0,
                "end": 1.5,
                "speaker": "S1",
                "text": "hola, ¿cómo estás?",
                "words": [{"word": "hola", "start": 0.0, "end": 0.5}],
            },
            {"start": 1.5, "end": 3.0, "speaker": "S2", "text": "bien — muy bien"},
        ]
    }
    tr = await make_transcription(session, user_id=user.id)
    tr.segments = nested_segments
    await session.flush()
    fetched = (
        await session.execute(select(Transcription).where(Transcription.id == tr.id))
    ).scalar_one()
    assert fetched.segments == nested_segments
    # Spot-check unicode + float preservation:
    assert fetched.segments["segments"][0]["text"] == "hola, ¿cómo estás?"
    assert fetched.segments["segments"][0]["start"] == 0.0


async def test_oauth_token_updated_at_advances_on_update(session):
    """
    Review fix H-7: OAuthToken.updated_at must advance on UPDATE (onupdate=now()).
    Without onupdate, server_default only fires on INSERT and the field freezes
    forever — Capa 2 token rotation would silently store stale timestamps.
    """
    import asyncio

    user = await make_user(session)
    tok = await make_oauth_token(session, user_id=user.id)
    initial_updated_at = tok.updated_at

    # Real-time tick — server-side now() has microsecond precision; sleep 10ms.
    await asyncio.sleep(0.01)

    tok.ms_access_token_encrypted = b"\xff\xfe"
    await session.flush()
    await session.refresh(tok)

    assert tok.updated_at > initial_updated_at, (
        "OAuthToken.updated_at must advance on UPDATE — onupdate=func.now() missing?"
    )
