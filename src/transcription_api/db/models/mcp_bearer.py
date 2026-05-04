"""McpBearer model — wiki/05_modelo_datos.md §2 `mcp_bearers`.

Bearer tokens emitted by the API for the user's Claude (via MCP) to call
tools/resources. Only the SHA-256 hash is stored; the cleartext is shown
once at issuance. The partial UNIQUE index `WHERE revoked_at IS NULL`
(at-most-one active bearer per user) is added in the Alembic migration —
SQLAlchemy core does not express partial indexes via column-level decls.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class McpBearer(Base):
    __tablename__ = "mcp_bearers"

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
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
