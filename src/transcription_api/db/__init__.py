"""Database layer — SQLAlchemy 2.0 async + asyncpg.

Spec: SPEC-capa1-postgres-orm-v1
Wiki refs: wiki/05_modelo_datos.md, wiki/ADR/ADR-008.md.

Re-exports the engine, session factory, and FastAPI dependency. Also imports
`db.models` so all ORM classes register on `Base.metadata` at import time
(needed for Alembic autogenerate to see them).
"""
from .base import Base
from .models import (  # noqa: F401  (registers models on Base.metadata)
    Image,
    McpBearer,
    OAuthToken,
    Transcription,
    UploadSession,
    User,
)
from .scoping import enable_per_user_scoping, set_session_user
from .session import async_session_factory, engine, get_session

__all__ = [
    "Base",
    "engine",
    "async_session_factory",
    "get_session",
    "enable_per_user_scoping",
    "set_session_user",
    "User",
    "OAuthToken",
    "McpBearer",
    "Transcription",
    "Image",
    "UploadSession",
]
