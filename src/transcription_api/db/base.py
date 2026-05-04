"""Declarative base + naming convention for Alembic-friendly migrations.

Spec: SPEC-capa1-postgres-orm-v1, ALT-2 (naming convention).

The naming convention pins index, constraint, and FK names so that
`alembic revision --autogenerate` produces stable, predictable identifiers
across runs. Without this, Alembic can re-emit the same constraint with a
different name on every migration, polluting diffs.
"""
from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Convention chosen to match wiki/05_modelo_datos.md §2: indexes use the
# `idx_*` prefix instead of SQLAlchemy's default `ix_*`. Unique constraints
# stay `uq_*`; primary keys `pk_*`; foreign keys `fk_*`; check constraints `ck_*`.
NAMING_CONVENTION = {
    "ix": "idx_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Common declarative base for all ORM models in this project."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
