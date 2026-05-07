"""Minimal in-memory factories for ORM models — test-only.

Spec: SPEC-capa1-postgres-orm-v1
Plan: docs/sesiones/2026-04-30-capa1-postgres-orm-plan.md (Batch 5).

We deliberately avoid Faker / model-bakery to keep test deps small and
make failures deterministic. Each factory:
- Generates fresh UUIDs / unique identifiers per call.
- Adds the row to the given session and flushes (autoflush would also work).
- Returns the persisted instance.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from transcription_api.db.models import (
    Image,
    McpBearer,
    OAuthToken,
    Transcription,
    UploadSession,
    User,
)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


async def make_user(
    session: AsyncSession,
    *,
    email: str | None = None,
    display_name: str = "Test User",
) -> User:
    user = User(
        id=uuid.uuid4(),
        microsoft_oid=uuid.uuid4(),
        email=email or f"u{secrets.token_hex(4)}@sandinas.test",
        display_name=display_name,
    )
    session.add(user)
    await session.flush()
    return user


async def make_oauth_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    access: bytes = b"\x01\x02",
    refresh: bytes = b"\x03\x04",
) -> OAuthToken:
    tok = OAuthToken(
        id=uuid.uuid4(),
        user_id=user_id,
        ms_access_token_encrypted=access,
        ms_refresh_token_encrypted=refresh,
        ms_access_expires_at=_utcnow() + timedelta(hours=1),
    )
    session.add(tok)
    await session.flush()
    return tok


async def make_bearer(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    token_hash: str | None = None,
    revoked_at: datetime | None = None,
    name: str | None = None,
) -> McpBearer:
    bearer = McpBearer(
        id=uuid.uuid4(),
        user_id=user_id,
        token_hash=token_hash or secrets.token_hex(16),
        revoked_at=revoked_at,
        name=name,
    )
    session.add(bearer)
    await session.flush()
    return bearer


async def make_transcription(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    audio_hash: str | None = None,
    text: str = "Reunión de prueba — agenda del día.",
    duration_seconds: float = 60.0,
    num_speakers: int = 2,
) -> Transcription:
    tr = Transcription(
        id=uuid.uuid4(),
        user_id=user_id,
        audio_hash=audio_hash or secrets.token_hex(16),
        original_filename="meeting.wav",
        original_size_bytes=1024,
        duration_seconds=Decimal(str(duration_seconds)),
        language="es",
        num_speakers=num_speakers,
        text_content=text,
        segments={"segments": []},
        extra_metadata={"model": "whisper-large-v3", "diarizer": "pyannote-3.1"},
    )
    session.add(tr)
    await session.flush()
    return tr


async def make_image(
    session: AsyncSession,
    *,
    transcription_id: uuid.UUID,
    user_id: uuid.UUID,
    filename: str = "screenshot.png",
    mime_type: str = "image/png",
    size_bytes: int = 1024,
) -> Image:
    img = Image(
        id=uuid.uuid4(),
        transcription_id=transcription_id,
        user_id=user_id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        file_path=f"blobs/{secrets.token_hex(8)}.png",
    )
    session.add(img)
    await session.flush()
    return img


async def make_upload_session(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    bearer_id: uuid.UUID,
    kind: str = "audio",
    expected_size_bytes: int = 4096,
    upload_bearer_hash: str | None = None,
    nonce: str | None = None,
    expires_at_offset_seconds: int = 600,
) -> UploadSession:
    """Capa 4 D-044: ``upload_bearer_hash`` is NOT NULL. Defaults to a
    random sha256 hex if the caller does not need to assert against the
    plaintext (Capa 1+2 callers don't care; Capa 4 callers pass an
    explicit hash to test the bearer match path)."""
    import hashlib

    up = UploadSession(
        id=uuid.uuid4(),
        user_id=user_id,
        bearer_id=bearer_id,
        nonce=nonce or secrets.token_hex(16),
        upload_bearer_hash=upload_bearer_hash
        or hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
        kind=kind,
        expected_size_bytes=expected_size_bytes,
        expires_at=_utcnow() + timedelta(seconds=expires_at_offset_seconds),
    )
    session.add(up)
    await session.flush()
    return up
