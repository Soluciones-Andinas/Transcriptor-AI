"""Alembic environment.

Spec: SPEC-capa1-postgres-orm-v1, AC-3 + ERR-5.

The runtime engine uses `postgresql+asyncpg`; Alembic's online mode runs
synchronously, so we convert the URL to `postgresql+psycopg` here. This
keeps prod 100% async while letting Alembic do its work without async loop
plumbing.

`target_metadata` points at `transcription_api.db.base.Base.metadata`. The
`db.models` package is imported as a side-effect so all 6 ORM models register
on that metadata before autogenerate compares against the DB.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from transcription_api.config import settings
from transcription_api.db import Base  # noqa: F401 — triggers model registration

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _alembic_url() -> str:
    """Return the sync URL Alembic should use.

    Priority: explicit `sqlalchemy.url` from CLI (`-x`) or .ini > settings.
    Always converted to `+psycopg` because Alembic's online mode is sync.

    M-8: validates the result is a Postgres URL using a sync driver.
    `replace('+asyncpg', '+psycopg')` is a no-op on URLs lacking that suffix
    (e.g. bare `postgresql://`), which silently picks the SQLAlchemy default
    driver. Catch that here so misconfigurations fail loudly.
    """
    explicit = config.get_main_option("sqlalchemy.url")
    url = explicit if explicit else settings.build_database_url()
    sync_url = url.replace("+asyncpg", "+psycopg")
    if "+asyncpg" in sync_url:
        raise RuntimeError(
            f"alembic_env: failed to convert async URL to sync: {sync_url!r}"
        )
    if not sync_url.startswith(("postgresql+psycopg://", "postgresql+psycopg2://")):
        raise RuntimeError(
            f"alembic_env: expected postgresql+psycopg(2) URL, got {sync_url.split('://')[0]!r}"
        )
    return sync_url


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL only)."""
    url = _alembic_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live DB."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _alembic_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
