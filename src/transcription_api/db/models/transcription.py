"""Transcription model — wiki/05_modelo_datos.md §2 `transcriptions`.

Persistent transcription history (no TTL — soft delete only). The composite
partial index on `(user_id, created_at DESC) WHERE deleted_at IS NULL` and
the GIN tsvector index for spanish full-text search are declared in the
Alembic migration (SQLAlchemy can't express either via column-level decls).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class Transcription(Base):
    __tablename__ = "transcriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    audio_hash: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    original_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # H-2: Numeric(10,2) returns Decimal at runtime (asyncpg + SQLAlchemy);
    # using Mapped[float] would lie to mypy and break Decimal-aware arithmetic.
    duration_seconds: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    num_speakers: Mapped[int] = mapped_column(Integer, nullable=False)
    text_content: Mapped[str] = mapped_column("text", Text, nullable=False)
    segments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
