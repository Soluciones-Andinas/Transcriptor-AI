"""Wiki §2 indexes — name, type, columns, partial predicate.

Spec: SPEC-capa1-postgres-orm-v1
Plan: docs/sesiones/2026-04-30-capa1-postgres-orm-plan.md
Covers: AC-6 (all indexes documented in wiki/05_modelo_datos.md §2 present in
real Postgres after `alembic upgrade head`).

Index name prefix conventions:
- `idx_*`  — non-unique B-tree, GIN, or composite indexes.
- `uq_*`   — UNIQUE constraints (Postgres implements them with a unique B-tree
            index that shows up in pg_indexes too).
- `pk_*`   — primary keys (skipped here; covered implicitly by AC-2).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.requires_docker


# (index_name, table_name, expected_using ('btree'|'gin'), partial_predicate fragment or None)
EXPECTED_INDEXES = [
    # transcriptions
    ("idx_transcriptions_user_created", "transcriptions", "btree", "deleted_at IS NULL"),
    # H-8: FTS index is partial on deleted_at IS NULL (review fix).
    ("idx_transcriptions_text_fts",     "transcriptions", "gin",   "deleted_at IS NULL"),
    ("idx_transcriptions_audio_hash",   "transcriptions", "btree", None),
    # mcp_bearers — both global UNIQUE on token_hash AND partial UNIQUE on (user_id) WHERE active
    ("uq_mcp_bearers_token_hash",       "mcp_bearers",    "btree", None),
    ("uq_mcp_bearers_active_per_user",  "mcp_bearers",    "btree", "revoked_at IS NULL"),
    # upload_sessions
    ("uq_upload_sessions_nonce",        "upload_sessions", "btree", None),
    ("idx_upload_sessions_status_expires", "upload_sessions", "btree", None),
]


@pytest.mark.parametrize(
    "index_name,table_name,using,partial",
    EXPECTED_INDEXES,
    ids=[i[0] for i in EXPECTED_INDEXES],
)
async def test_index_present(engine, index_name, table_name, using, partial):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-6 — wiki §2 indexes present with correct table, access method,
    and partial predicate after migration applied.
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT tablename, indexdef FROM pg_indexes "
                "WHERE schemaname='public' AND indexname = :name"
            ),
            {"name": index_name},
        )
        row = result.first()

    assert row is not None, f"index {index_name} not found in pg_indexes"
    actual_table, actual_def = row[0], row[1]
    assert actual_table == table_name, (
        f"{index_name} on table {actual_table}, expected {table_name}"
    )
    # pg_indexes.indexdef is `CREATE [UNIQUE] INDEX <name> ON <table> USING <method> (...)`
    # — `USING` is always uppercase, the method name (btree/gin/...) always lowercase.
    assert f"USING {using}" in actual_def, (
        f"{index_name} does not use {using}: {actual_def}"
    )
    if partial is not None:
        assert "WHERE" in actual_def.upper(), (
            f"{index_name} should be a partial index (WHERE {partial}): {actual_def}"
        )
        # Postgres normalizes case in pg_indexes output; do a loose containment check
        assert partial.lower().replace(" ", "") in actual_def.lower().replace(" ", ""), (
            f"{index_name} predicate mismatch — expected '{partial}' in {actual_def}"
        )


async def test_transcription_user_created_index_orders_desc(engine):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-6 — composite index `(user_id, created_at DESC)` orders created_at
    descending so the recent-meetings query is index-only.
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname='public' AND indexname='idx_transcriptions_user_created'"
            )
        )
        row = result.first()
    assert row is not None
    indexdef = row[0]
    assert "created_at DESC" in indexdef, (
        f"index must order created_at DESC: {indexdef}"
    )
