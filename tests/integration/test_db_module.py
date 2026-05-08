"""Integration tests for the `transcription_api.db` module foundation.

Spec: SPEC-capa1-postgres-orm-v1
Plan: docs/sesiones/2026-04-30-capa1-postgres-orm-plan.md
Covers: AC-1 (module importable), AC-2 (6 models metadata), AC-7 (engine from settings).
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# AC-1 — db module importable
# ---------------------------------------------------------------------------
def test_db_module_imports():
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-1 — `from transcription_api.db import engine, async_session_factory, get_session`
    must succeed and expose objects of the expected runtime types.
    """
    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

    from transcription_api.db import async_session_factory, engine, get_session

    assert isinstance(engine, AsyncEngine), "engine must be AsyncEngine"
    assert isinstance(async_session_factory, async_sessionmaker), (
        "async_session_factory must be async_sessionmaker"
    )
    assert callable(get_session), "get_session must be a callable dependency"


# ---------------------------------------------------------------------------
# AC-2 — 6 models declared with correct metadata
# ---------------------------------------------------------------------------
EXPECTED_MODELS = [
    ("User", "users", []),
    ("OAuthToken", "oauth_tokens", ["user_id"]),
    ("McpBearer", "mcp_bearers", ["user_id"]),
    ("Transcription", "transcriptions", ["user_id"]),
    ("Image", "images", ["user_id", "transcription_id"]),
    ("UploadSession", "upload_sessions", ["user_id"]),
]


@pytest.mark.parametrize(
    "model_name,expected_tablename,fk_columns",
    EXPECTED_MODELS,
    ids=[m[0] for m in EXPECTED_MODELS],
)
def test_models_metadata(model_name, expected_tablename, fk_columns):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-2 — every model has the expected `__tablename__` and FKs to `users`
    declare `ondelete="CASCADE"` per wiki/05_modelo_datos.md §2.
    """
    import transcription_api.db.models as models

    model = getattr(models, model_name, None)
    assert model is not None, f"{model_name} must be re-exported from db.models"
    assert model.__tablename__ == expected_tablename, (
        f"{model_name}.__tablename__ must be '{expected_tablename}'"
    )

    for col_name in fk_columns:
        col = model.__table__.columns.get(col_name)
        assert col is not None, f"{model_name} must declare column {col_name}"
        fks = list(col.foreign_keys)
        assert fks, f"{model_name}.{col_name} must be a foreign key"
        for fk in fks:
            if fk.column.table.name == "users":
                assert fk.ondelete == "CASCADE", (
                    f"{model_name}.{col_name} FK to users must have ondelete=CASCADE"
                )


# ---------------------------------------------------------------------------
# AC-7 — engine + session factory derived from settings
# ---------------------------------------------------------------------------
@pytest.mark.no_engine_override
def test_engine_from_settings():
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-7 — engine.url uses asyncpg dialect and pool sizing comes from
    settings.db_pool_size / settings.db_pool_max_overflow.

    Review fix M-2: assert against the configured maximum (`pool._pool.maxsize`
    on asyncpg's QueuePool) rather than the *current* `pool.size()`, which is 0
    until the first checkout under lazy initialization.
    """
    from transcription_api.config import settings
    from transcription_api.db import engine

    assert engine.url.drivername == "postgresql+asyncpg", (
        f"expected postgresql+asyncpg, got {engine.url.drivername}"
    )
    # Pool class is AsyncAdaptedQueuePool — it wraps a sync QueuePool whose
    # `_pool.maxsize` is the configured `pool_size`.
    assert engine.pool.__class__.__name__ in (
        "AsyncAdaptedQueuePool", "QueuePool",
    ), f"unexpected pool class: {engine.pool.__class__.__name__}"
    # The QueuePool's underlying queue maxsize equals pool_size.
    inner_maxsize = engine.pool._pool.maxsize  # type: ignore[attr-defined]
    assert inner_maxsize == settings.db_pool_size, (
        f"engine pool maxsize={inner_maxsize} but settings.db_pool_size={settings.db_pool_size}"
    )
    assert engine.url.username == settings.postgres_user
    assert engine.url.host == settings.postgres_host
    assert engine.url.database == settings.postgres_db
