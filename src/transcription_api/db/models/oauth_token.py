"""OAuthToken model — wiki/05_modelo_datos.md §2 `oauth_tokens`.

Stores Microsoft Entra ID access + refresh tokens (encrypted with
`OAUTH_TOKEN_ENC_KEY` at the application layer). The encrypt/decrypt helpers
live in Capa 2 (Auth); this model only declares the BYTEA columns.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # one active token row per user
    )
    ms_access_token_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ms_refresh_token_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ms_access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
