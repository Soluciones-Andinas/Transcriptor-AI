"""``search_my_transcriptions`` FTS predicate compiles with a regconfig cast.

Spec: SPEC-capa4-mcp-v1
Covers (G3 — review-fixes Tier 1):
- AC-4 perf — the ORM expression for the FTS WHERE clause must compile
  to use ``'spanish'::regconfig``, not the bare TEXT literal
  ``'spanish'``. Without the cast, Postgres' planner does not pick the
  GIN functional index ``idx_transcriptions_text_fts`` (the index was
  created on the ``regconfig`` form during the Capa 1 migration), and
  silently falls back to a sequential scan on the ``text_content``
  column. Asserting the rendered SQL keeps the regression detectable
  without spinning a testcontainer for an EXPLAIN check.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects import postgresql


def test_search_query_renders_with_regconfig_cast() -> None:
    """The ``_build_fts_predicate`` helper must emit
    ``to_tsvector('spanish'::regconfig, ...) @@ plainto_tsquery('spanish'::regconfig, ...)``
    so the GIN functional index is eligible. The plain string
    ``'spanish'`` (TEXT bind) is the regression we are guarding against.
    """
    from transcription_api.db.models import Transcription
    from transcription_api.mcp.tools.transcription import _build_fts_predicate

    stmt = select(Transcription.id).where(
        _build_fts_predicate(Transcription, "arquitectura")
    )
    sql = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "::regconfig" in sql, sql
