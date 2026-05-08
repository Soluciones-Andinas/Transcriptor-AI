"""UploadSession model — wiki/05_modelo_datos.md §2 `upload_sessions`.

Ephemeral binding between an MCP tool call (`request_upload_url`) and the
REST endpoint (`POST /api/upload`). The composite (status, expires_at) index
is added by the Alembic migration for cleanup-job efficiency.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class UploadSession(Base):
    __tablename__ = "upload_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bearer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("mcp_bearers.id"),  # no cascade — keep audit trail when bearer revoked
        nullable=False,
    )
    nonce: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Capa 4 D-044 — SHA-256(plaintext) hex of the ephemeral bearer emitted
    # by ``request_upload_url`` (RF-MCP-01 step 3). ``POST /api/upload``
    # validates the incoming ``Authorization: Bearer <plaintext>`` against
    # this stored hash with ``hmac.compare_digest`` (RF-MCP-03 step 4).
    # Plaintext is shown to the client once and never persisted.
    upload_bearer_hash: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # 'audio' | 'image'
    transcription_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("transcriptions.id"),
        nullable=True,
    )
    expected_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'requested'"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Cleanup-job index for finding expired sessions to GC.
    __table_args__ = (
        Index("idx_upload_sessions_status_expires", "status", "expires_at"),
    )
