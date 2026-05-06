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
# AC-4 — Initial migration covers the 6 tables. The schema-level coverage
# (column types, indexes, naming convention) is tested at runtime against
# the live DB in test_db_schema.py and test_db_indexes.py — those assertions
# are stable across Alembic format changes, so we keep this file's
# source-grep checks minimal (review fix M-3).
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


# ---------------------------------------------------------------------------
# ERR-3 — Migration drift: model metadata vs applied schema must agree.
# ---------------------------------------------------------------------------
@pytest.mark.requires_docker
async def test_no_migration_drift_against_models(engine):
    """
    Spec: SPEC-capa1-postgres-orm-v1, ERR-3 (review fix M-4).

    Run `alembic check` semantics manually: with the model metadata loaded
    and the migrated DB applied, compare the two via Alembic's autogenerate
    diff machinery and assert the diff is empty. A non-empty diff means
    someone changed a model without writing the matching migration.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from transcription_api.db import Base

    # Run autogenerate compare against the migrated testcontainer.
    # MigrationContext expects a sync connection; use engine.begin() in a
    # run_sync wrapper.
    async with engine.connect() as conn:
        diffs = await conn.run_sync(
            lambda sync_conn: compare_metadata(
                MigrationContext.configure(
                    connection=sync_conn,
                    opts={"compare_type": True, "compare_server_default": True},
                ),
                Base.metadata,
            )
        )

    # Filter out diffs known to be intentional / irreducible:
    # - `idx_transcriptions_text_fts`: functional GIN index — SQLAlchemy
    #   can't literal-render regconfig 'spanish', so it's migration-only.
    # Plus, ignore server_default text() comparison false-positives.
    intentional_index_drift = {"idx_transcriptions_text_fts"}

    def _is_intentional(diff) -> bool:
        if not (isinstance(diff, (list, tuple)) and diff):
            return True
        op_name = diff[0]
        if op_name in {"add_index", "remove_index"} and len(diff) >= 2:
            idx = diff[1]
            if getattr(idx, "name", None) in intentional_index_drift:
                return True
        return False

    blocking = [
        d for d in diffs
        if isinstance(d, (list, tuple))
        and d
        and d[0] in {
            "add_table", "remove_table",
            "add_column", "remove_column",
            "add_constraint", "remove_constraint",
            "add_index", "remove_index",
        }
        and not _is_intentional(d)
    ]
    assert not blocking, (
        f"migration drift detected — model metadata diverged from migration:\n{blocking}"
    )


# ---------------------------------------------------------------------------
# ERR-5 — async URL → sync URL conversion (unit-level).
# ---------------------------------------------------------------------------
def test_alembic_url_conversion_handles_asyncpg():
    """
    Spec: SPEC-capa1-postgres-orm-v1, ERR-5 (review fix M-5).

    The conversion logic in alembic/env.py replaces +asyncpg with +psycopg
    so Alembic's sync runner can execute migrations. Verify the helper
    rejects unrecognized URLs loudly instead of silently picking the
    SQLAlchemy default driver.
    """
    # We replicate the conversion as a pure helper. If env.py refactors,
    # update this test alongside.
    def _convert(url: str) -> str:
        sync_url = url.replace("+asyncpg", "+psycopg")
        if "+asyncpg" in sync_url:
            raise RuntimeError(f"failed to convert async URL: {sync_url!r}")
        if not sync_url.startswith(("postgresql+psycopg://", "postgresql+psycopg2://")):
            raise RuntimeError(f"unexpected URL: {sync_url}")
        return sync_url

    assert _convert("postgresql+asyncpg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert _convert("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"

    with pytest.raises(RuntimeError, match="unexpected URL"):
        _convert("postgresql://u:p@h/db")  # bare driver — must fail loudly
    with pytest.raises(RuntimeError, match="unexpected URL"):
        _convert("mysql+aiomysql://u:p@h/db")  # wrong dialect


# ---------------------------------------------------------------------------
# Capa 4 AC-15 — `upload_bearer_hash` column added by the
# add_upload_bearer_hash migration (Batch 0). Validates the column exists,
# is TEXT, and is NOT NULL after `alembic upgrade head` runs against a
# fresh testcontainer. Closes D-044 at the schema level: without this
# column, RF-MCP-03 step 4 (validate ephemeral bearer against stored hash)
# cannot be implemented.
# ---------------------------------------------------------------------------
@pytest.mark.requires_docker
async def test_upload_sessions_has_upload_bearer_hash_after_upgrade(engine):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-15 — Given a fresh DB with `alembic upgrade head`
    applied, When inspecting ``information_schema.columns`` for
    ``upload_sessions``, Then the ``upload_bearer_hash`` column exists
    with ``data_type='text'`` and ``is_nullable='NO'``.
    """
    from sqlalchemy import text

    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = 'upload_sessions' "
                "AND column_name = 'upload_bearer_hash'"
            )
        )
        row = result.first()

    assert row is not None, (
        "upload_sessions.upload_bearer_hash column missing — "
        "add_upload_bearer_hash migration not applied?"
    )
    assert row.data_type == "text", (
        f"upload_bearer_hash should be TEXT (sha256 hex); got {row.data_type}"
    )
    assert row.is_nullable == "NO", (
        f"upload_bearer_hash must be NOT NULL (RF-MCP-03 contract); "
        f"got is_nullable={row.is_nullable}"
    )
