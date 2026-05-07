"""``search_my_transcriptions`` tool — RF-MCP-05.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 4 task 4.2):
- AC-4 — Spanish full-text search via the GIN tsvector index
  (``idx_transcriptions_text_fts``). Returns matching rows ranked
  by ``ts_rank`` with a ``ts_headline``-formatted snippet showing
  the matched terms.
- Cross-user isolation — same listener-driven AND-inject pattern as
  list; the raw SQL query uses ``Transcription`` mapper so the
  listener sees the per-user model and applies the predicate.
- Validation — empty / oversize query -> ``INVALID_PARAMETER``;
  ``limit > 50`` clamped to 50.

Tests run unit-style; ``requires_docker`` because the Postgres
testcontainer is needed for ``to_tsvector('spanish', ...)`` +
``plainto_tsquery``. Spanish text-search config is built into
PostgreSQL since 8.3 — no extension needed.
"""
from __future__ import annotations

import secrets
from uuid import UUID

import pytest

from tests.factories import make_bearer, make_transcription, make_user
from transcription_api.auth.mcp_bearer import generate_bearer

pytestmark = pytest.mark.requires_docker


# ---------------------------------------------------------------------------
# Helpers (mirror list test file)
# ---------------------------------------------------------------------------
async def _seed_user_with_bearer(session, *, email_suffix: str):
    user = await make_user(session, email=f"u-{email_suffix}@x")
    plain, token_hash = generate_bearer()
    bearer = await make_bearer(session, user_id=user.id, token_hash=token_hash)
    return user, bearer


def _arm_context(user_id: UUID, bearer_id: UUID):
    from transcription_api.mcp.middleware import (
        _current_bearer_id,
        _current_user_id,
    )

    user_token = _current_user_id.set(user_id)
    bearer_token = _current_bearer_id.set(bearer_id)

    def _reset():
        _current_user_id.reset(user_token)
        _current_bearer_id.reset(bearer_token)

    return _reset


def _is_tool_error(exc, expected_code: str) -> bool:
    from mcp.shared.exceptions import McpError

    if not isinstance(exc, McpError):
        return False
    data = getattr(exc.error, "data", None) if hasattr(exc, "error") else None
    return isinstance(data, dict) and data.get("error_code") == expected_code


# ---------------------------------------------------------------------------
# AC-4 — FTS happy path
# ---------------------------------------------------------------------------
async def test_search_returns_matching_rows_with_rank_and_snippet(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-4 — Given a transcription whose text contains the
    query term, When search_my_transcriptions runs, Then the result
    list has the row with rank > 0 and a snippet that contains the
    query term (case-insensitive Spanish stemming via 'spanish'
    config).
    """
    from transcription_api.mcp.tools.transcription import search_my_transcriptions

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"fts-{secrets.token_hex(2)}"
    )
    target = await make_transcription(
        session,
        user_id=user.id,
        text="La arquitectura del sistema usa microservicios y Postgres",
        audio_hash="match-" + "0" * 58,
    )
    # Decoy with no match.
    await make_transcription(
        session,
        user_id=user.id,
        text="Reunión sobre presupuesto y agenda del trimestre",
        audio_hash="decoy-" + "0" * 58,
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        results = await search_my_transcriptions(query="arquitectura")
    finally:
        reset()

    assert isinstance(results, list)
    assert len(results) == 1, f"expected 1 match; got {results}"
    hit = results[0]
    assert hit["id"] == str(target.id)
    assert hit["rank"] > 0
    # ts_headline wraps matches in <b>...</b> by default.
    assert "arquitectura" in hit["snippet"].lower()


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------
async def test_search_does_not_leak_other_users_matches(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-4 + ADR-014/015 — Given user B has a row matching
    the query and user A does not, When user A searches, Then the
    result is empty (B's row filtered by the listener).
    """
    from transcription_api.mcp.tools.transcription import search_my_transcriptions

    alice, alice_bearer = await _seed_user_with_bearer(
        session, email_suffix=f"alice-{secrets.token_hex(2)}"
    )
    bob, _ = await _seed_user_with_bearer(
        session, email_suffix=f"bob-{secrets.token_hex(2)}"
    )
    await make_transcription(
        session,
        user_id=bob.id,
        text="Bob habla sobre la arquitectura",
        audio_hash="bob-" + "0" * 60,
    )
    await session.commit()

    reset = _arm_context(alice.id, alice_bearer.id)
    try:
        results = await search_my_transcriptions(query="arquitectura")
    finally:
        reset()

    assert results == [], (
        "Bob's match leaked across users; listener AND-inject failed?"
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
async def test_search_rejects_empty_query(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_PARAMETER — empty / whitespace-only
    query.
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.tools.transcription import search_my_transcriptions

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"empty-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await search_my_transcriptions(query="   ")
        assert _is_tool_error(exc.value, "INVALID_PARAMETER")
    finally:
        reset()


async def test_search_rejects_oversized_query(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_PARAMETER — query > 200 chars (DOS
    guard against pathological FTS parses).
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.tools.transcription import search_my_transcriptions

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"long-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await search_my_transcriptions(query="x" * 201)
        assert _is_tool_error(exc.value, "INVALID_PARAMETER")
    finally:
        reset()


async def test_search_clamps_oversized_limit_to_50(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: pagination grace window (G5 review-fix) — values in
    ``(MAX, MAX*2]`` silently clamp to ``MAX``; absurd values above
    ``MAX*2`` raise (covered by ``test_search_invalid_limit_raises``).
    FTS uses ``MAX=50`` so the grace window is ``(50, 100]``.
    """
    from transcription_api.mcp.tools.transcription import search_my_transcriptions

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"clamp-{secrets.token_hex(2)}"
    )
    await make_transcription(
        session,
        user_id=user.id,
        text="contenido con la palabra arquitectura",
        audio_hash="clmp-" + "0" * 59,
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        # 80 lies in (50, 100] grace window -> silently clamped to 50.
        results = await search_my_transcriptions(query="arquitectura", limit=80)
    finally:
        reset()

    # 1 match but no error — proves the clamp didn't break the path.
    assert len(results) == 1


# ---------------------------------------------------------------------------
# G5 — search params hardening
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"query": "arquitectura", "limit": -1},
        {"query": "arquitectura", "limit": 0},
        {"query": "arquitectura", "limit": 9999},  # > _SEARCH_LIMIT_MAX*2
    ],
)
async def test_search_invalid_limit_raises(session, bad_kwargs):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_PARAMETER — out-of-band ``limit`` raises
    instead of being silently clamped. Empty / oversize ``query`` is
    already covered by ``test_search_empty_query_raises`` /
    ``test_search_oversize_query_raises``; this drives the limit policy.
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.tools.transcription import search_my_transcriptions

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"searchparams-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await search_my_transcriptions(**bad_kwargs)
        assert _is_tool_error(exc.value, "INVALID_PARAMETER")
    finally:
        reset()
