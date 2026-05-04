"""Alembic integration tests — config + migration content.

Spec: SPEC-capa1-postgres-orm-v1
Plan: docs/sesiones/2026-04-30-capa1-postgres-orm-plan.md
Covers: AC-3 (alembic config loadable), AC-4 (initial migration content) + ALT-2 (naming).
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
ALEMBIC_DIR = REPO_ROOT / "alembic"
VERSIONS_DIR = ALEMBIC_DIR / "versions"


# ---------------------------------------------------------------------------
# AC-3 — Alembic initialized and operative
# ---------------------------------------------------------------------------
def test_alembic_config():
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-3 — alembic.ini exists, env.py loads Base.metadata as target_metadata,
    and the script_directory is resolvable.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    assert ALEMBIC_INI.is_file(), f"missing {ALEMBIC_INI}"
    config = Config(str(ALEMBIC_INI))
    script = ScriptDirectory.from_config(config)
    # ScriptDirectory.from_config raises if alembic dir / versions dir is wrong
    assert Path(script.dir).resolve() == ALEMBIC_DIR.resolve()

    # env.py must reference Base.metadata. We assert by importing the env module
    # path; full execution requires a DB (covered in AC-5/T6).
    env_py = ALEMBIC_DIR / "env.py"
    assert env_py.is_file()
    content = env_py.read_text()
    assert "from transcription_api.db" in content, (
        "env.py must import from transcription_api.db to register Base.metadata"
    )
    assert "target_metadata" in content
    assert "Base.metadata" in content


# ---------------------------------------------------------------------------
# AC-4 + ALT-2 — Initial migration content
# ---------------------------------------------------------------------------
EXPECTED_TABLES = [
    "users",
    "oauth_tokens",
    "mcp_bearers",
    "transcriptions",
    "images",
    "upload_sessions",
]


@pytest.fixture(scope="module")
def initial_migration_text() -> str:
    """Locate the single initial migration file and return its source text."""
    assert VERSIONS_DIR.is_dir(), f"missing {VERSIONS_DIR}"
    migrations = sorted(VERSIONS_DIR.glob("*.py"))
    migrations = [m for m in migrations if m.name != "__init__.py"]
    assert len(migrations) >= 1, "no migration files in alembic/versions/"
    # Initial migration is the one that creates `users` and has no down_revision
    for m in migrations:
        text = m.read_text()
        if "create_table('users'" in text or 'create_table("users"' in text:
            return text
    pytest.fail("could not locate initial migration creating 'users' table")


@pytest.mark.parametrize("table_name", EXPECTED_TABLES)
def test_initial_migration_creates_table(initial_migration_text, table_name):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-4 — initial migration emits op.create_table for each of the 6 tables.
    """
    quoted_single = f"create_table('{table_name}'"
    quoted_double = f'create_table("{table_name}"'
    assert quoted_single in initial_migration_text or quoted_double in initial_migration_text, (
        f"migration must call op.create_table('{table_name}')"
    )


def test_initial_migration_uses_naming_convention(initial_migration_text):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: ALT-2 — naming convention applied (pk_*, ix_*, fk_*, uq_*).
    Autogenerate uses the metadata's naming_convention so identifiers are stable.
    """
    # At least one PK with the convention, and at least one FK with the convention
    assert "pk_users" in initial_migration_text or "pk_transcriptions" in initial_migration_text
    assert "fk_oauth_tokens_user_id_users" in initial_migration_text \
        or "fk_mcp_bearers_user_id_users" in initial_migration_text \
        or "fk_transcriptions_user_id_users" in initial_migration_text
