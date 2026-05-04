"""Schema verification tests against a real Postgres (testcontainers).

Spec: SPEC-capa1-postgres-orm-v1
Plan: docs/sesiones/2026-04-30-capa1-postgres-orm-plan.md
Covers: AC-5 (column types match wiki §2). AC-6 (indexes) is in test_db_indexes.py (T7).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.requires_docker


# (table, column, expected udt_name from information_schema.columns)
EXPECTED_COLUMN_TYPES = [
    # users
    ("users", "id", "uuid"),
    ("users", "microsoft_oid", "uuid"),
    ("users", "email", "text"),
    ("users", "display_name", "text"),
    ("users", "created_at", "timestamptz"),
    ("users", "last_login_at", "timestamptz"),
    # oauth_tokens
    ("oauth_tokens", "user_id", "uuid"),
    ("oauth_tokens", "ms_access_token_encrypted", "bytea"),
    ("oauth_tokens", "ms_refresh_token_encrypted", "bytea"),
    ("oauth_tokens", "ms_access_expires_at", "timestamptz"),
    # mcp_bearers
    ("mcp_bearers", "token_hash", "text"),
    ("mcp_bearers", "revoked_at", "timestamptz"),
    # transcriptions
    ("transcriptions", "audio_hash", "text"),
    ("transcriptions", "original_size_bytes", "int8"),
    ("transcriptions", "duration_seconds", "numeric"),
    ("transcriptions", "num_speakers", "int4"),
    ("transcriptions", "text", "text"),
    ("transcriptions", "segments", "jsonb"),
    ("transcriptions", "metadata", "jsonb"),
    ("transcriptions", "deleted_at", "timestamptz"),
    # images
    ("images", "transcription_id", "uuid"),
    ("images", "file_path", "text"),
    ("images", "size_bytes", "int8"),
    # upload_sessions
    ("upload_sessions", "nonce", "text"),
    ("upload_sessions", "kind", "text"),
    ("upload_sessions", "expected_size_bytes", "int8"),
    ("upload_sessions", "status", "text"),
    ("upload_sessions", "expires_at", "timestamptz"),
]


@pytest.mark.parametrize(
    "table,column,expected_udt",
    EXPECTED_COLUMN_TYPES,
    ids=[f"{t}.{c}" for t, c, _ in EXPECTED_COLUMN_TYPES],
)
async def test_schema_column_types_match_wiki(engine, table, column, expected_udt):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-5 — after `alembic upgrade head`, every column in the wiki §2
    schema has its expected Postgres type (jsonb, bytea, numeric, timestamptz, ...).
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        )
        row = result.first()
        assert row is not None, f"column {table}.{column} not found"
        assert row[0] == expected_udt, (
            f"{table}.{column} expected udt_name={expected_udt}, got {row[0]}"
        )


async def test_numeric_precision_and_scale(engine):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-5 — `transcriptions.duration_seconds` is exactly NUMERIC(10,2).
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT numeric_precision, numeric_scale FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='transcriptions' "
                "AND column_name='duration_seconds'"
            )
        )
        precision, scale = result.first()
        assert precision == 10
        assert scale == 2


async def test_all_six_tables_present(engine):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-5 — the 6 wiki §2 tables exist in public schema after migration.
    """
    expected = {"users", "oauth_tokens", "mcp_bearers", "transcriptions", "images", "upload_sessions"}
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            )
        )
        actual = {r[0] for r in result}
    missing = expected - actual
    assert not missing, f"missing tables: {missing}"
