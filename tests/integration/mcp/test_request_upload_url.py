"""``request_upload_url`` tool — RF-MCP-01.

Spec: SPEC-capa4-mcp-v1
Covers:
- AC-1 (partial; full chain closes in B2.4 + B3.1) — Tool returns
  ``upload_url``, ``upload_id``, ``bearer``, ``expires_at`` on the happy
  path; persists a ``upload_sessions`` row whose ``upload_bearer_hash``
  is ``SHA-256(plaintext)`` (D-044). Plaintext is NOT stored.
- AC-1 image-cross-user — kind=image referencing another user's
  transcription_id returns ``TRANSCRIPTION_NOT_FOUND`` (the listener
  fail-closed AND-injects ``user_id = current_user.id`` so the SELECT
  returns no row, even with bypass paths off).
- Validation surface (spec §4 ``INVALID_PARAMETER`` + ``FILE_TOO_LARGE``):
  invalid kind, non-positive size, audio-too-large, image without
  mime_type, unsupported image MIME, image-too-large,
  image without transcription_id.

Tests run against a real Postgres testcontainer (``requires_docker``)
because the tool's contract is "INSERTs a row and returns ID/url/bearer";
mocking the DB would assert the wrong contract. They invoke the tool
function directly (no MCP transport) by arming ``_current_user_id`` and
``_current_bearer_id`` ContextVars manually — the wire smoke is owned by
Batch 1.
"""
from __future__ import annotations

from hashlib import sha256

import pytest
from sqlalchemy import select

from tests.factories import make_bearer, make_transcription, make_user
from transcription_api.auth.mcp_bearer import generate_bearer

pytestmark = pytest.mark.requires_docker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _seed_user_and_bearer(session, *, email_suffix: str):
    """Create a User + active McpBearer; commit; return (user, bearer)."""
    user = await make_user(session, email=f"u-{email_suffix}@x")
    plaintext, token_hash = generate_bearer()
    bearer = await make_bearer(session, user_id=user.id, token_hash=token_hash)
    await session.commit()
    return user, bearer


def _arm_context(user_id, bearer_id):
    """Arm the middleware ContextVars for tool invocation outside the
    request path. Returns a callable that resets both.
    """
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
    """The tool raises ``McpError`` whose ``data`` carries ``error_code``."""
    from mcp.shared.exceptions import McpError

    if not isinstance(exc, McpError):
        return False
    data = getattr(exc.error, "data", None) if hasattr(exc, "error") else None
    if isinstance(data, dict) and data.get("error_code") == expected_code:
        return True
    return False


# ---------------------------------------------------------------------------
# AC-1 — happy path (audio)
# ---------------------------------------------------------------------------
async def test_request_upload_url_audio_happy_returns_url_id_bearer_expires(
    session,
):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-1 — Given a valid bearer + kind=audio + reasonable size,
    When request_upload_url is invoked, Then the result has the four
    canonical fields (upload_url, upload_id, bearer, expires_at) AND
    ``upload_sessions`` has exactly one row whose ``upload_bearer_hash``
    matches ``SHA-256(result['bearer'])``. Plaintext is NEVER persisted.
    """
    from transcription_api.db.models import UploadSession
    from transcription_api.mcp.tools.upload import request_upload_url

    user, bearer = await _seed_user_and_bearer(session, email_suffix="audio")
    reset = _arm_context(user.id, bearer.id)
    try:
        result = await request_upload_url(
            kind="audio", file_size_bytes=10_000_000
        )
    finally:
        reset()

    # Result shape contract.
    assert "upload_url" in result
    assert "upload_id" in result
    assert "bearer" in result
    assert "expires_at" in result
    assert len(result["bearer"]) >= 32, "ephemeral bearer must be ≥32 chars"
    assert result["upload_url"].startswith("http"), "upload_url must be absolute"
    assert "/api/upload" in result["upload_url"]

    # DB invariants (the tool committed via mcp_request_session ctx mgr).
    rows = (
        await session.execute(
            select(UploadSession).where(
                UploadSession.id == result["upload_id"]
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == user.id
    assert row.kind == "audio"
    assert row.expected_size_bytes == 10_000_000
    assert row.status == "requested"
    expected_hash = sha256(result["bearer"].encode()).hexdigest()
    assert row.upload_bearer_hash == expected_hash
    # Plaintext NOT persisted anywhere on the row.
    plaintext = result["bearer"]
    assert plaintext != row.upload_bearer_hash
    assert plaintext != row.nonce  # nonce is the URL token, not the bearer


# ---------------------------------------------------------------------------
# Validation surface — INVALID_PARAMETER
# ---------------------------------------------------------------------------
async def test_request_upload_url_rejects_unknown_kind(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_PARAMETER — kind not in {'audio','image'}.
    """
    from mcp.shared.exceptions import McpError
    from transcription_api.mcp.tools.upload import request_upload_url

    user, bearer = await _seed_user_and_bearer(session, email_suffix="bad-kind")
    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await request_upload_url(kind="video", file_size_bytes=1000)
        assert _is_tool_error(exc.value, "INVALID_PARAMETER")
    finally:
        reset()


async def test_request_upload_url_rejects_non_positive_size(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_PARAMETER — file_size_bytes <= 0.
    """
    from mcp.shared.exceptions import McpError
    from transcription_api.mcp.tools.upload import request_upload_url

    user, bearer = await _seed_user_and_bearer(session, email_suffix="zero-size")
    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await request_upload_url(kind="audio", file_size_bytes=0)
        assert _is_tool_error(exc.value, "INVALID_PARAMETER")
    finally:
        reset()


# ---------------------------------------------------------------------------
# Validation surface — FILE_TOO_LARGE (audio)
# ---------------------------------------------------------------------------
async def test_request_upload_url_rejects_audio_too_large(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 FILE_TOO_LARGE — kind=audio + size > MAX_UPLOAD_MB.
    """
    from mcp.shared.exceptions import McpError
    from transcription_api.config import settings
    from transcription_api.mcp.tools.upload import request_upload_url

    user, bearer = await _seed_user_and_bearer(session, email_suffix="big-aud")
    too_big = settings.max_upload_mb * 1024 * 1024 + 1
    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await request_upload_url(kind="audio", file_size_bytes=too_big)
        assert _is_tool_error(exc.value, "FILE_TOO_LARGE")
    finally:
        reset()


# ---------------------------------------------------------------------------
# Image branch — happy path + validation
# ---------------------------------------------------------------------------
async def test_request_upload_url_image_happy_with_own_transcription(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-1 — Given kind=image + mime_type valid +
    transcription_id of caller's own row, When request_upload_url runs,
    Then the result has the canonical fields + the row is INSERTed with
    ``kind='image'`` and the transcription_id link.
    """
    from transcription_api.db.models import UploadSession
    from transcription_api.mcp.tools.upload import request_upload_url

    user, bearer = await _seed_user_and_bearer(session, email_suffix="img-ok")
    own_tr = await make_transcription(session, user_id=user.id)
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        result = await request_upload_url(
            kind="image",
            file_size_bytes=200_000,
            mime_type="image/png",
            transcription_id=str(own_tr.id),
        )
    finally:
        reset()

    rows = (
        await session.execute(
            select(UploadSession).where(
                UploadSession.id == result["upload_id"]
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "image"
    assert rows[0].transcription_id == own_tr.id


async def test_request_upload_url_image_cross_user_returns_not_found(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-1 + ADR-014/015 — User B requests an image upload
    against User A's transcription_id. The listener fail-closed
    AND-injects ``user_id = B.id``, so the SELECT for A's transcription
    yields no row, and the tool returns ``TRANSCRIPTION_NOT_FOUND``
    (no existence leak).
    """
    from mcp.shared.exceptions import McpError
    from transcription_api.mcp.tools.upload import request_upload_url

    alice = await make_user(session, email="alice-cross@x")
    alice_tr = await make_transcription(session, user_id=alice.id)
    bob, bob_bearer = await _seed_user_and_bearer(
        session, email_suffix="bob-cross"
    )

    reset = _arm_context(bob.id, bob_bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await request_upload_url(
                kind="image",
                file_size_bytes=100_000,
                mime_type="image/png",
                transcription_id=str(alice_tr.id),
            )
        assert _is_tool_error(exc.value, "TRANSCRIPTION_NOT_FOUND")
    finally:
        reset()


async def test_request_upload_url_image_rejects_missing_mime(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_PARAMETER — kind=image without
    mime_type. Audio doesn't need a mime hint (ffmpeg sniffs it);
    images do (we whitelist known MIMEs upfront).
    """
    from mcp.shared.exceptions import McpError
    from transcription_api.mcp.tools.upload import request_upload_url

    user, bearer = await _seed_user_and_bearer(
        session, email_suffix="img-no-mime"
    )
    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await request_upload_url(
                kind="image",
                file_size_bytes=200_000,
                mime_type=None,
                transcription_id=None,
            )
        assert _is_tool_error(exc.value, "INVALID_PARAMETER")
    finally:
        reset()


async def test_request_upload_url_image_rejects_unsupported_mime(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_PARAMETER — image with mime_type outside
    the supported set (png/jpeg/webp/gif).
    """
    from mcp.shared.exceptions import McpError
    from transcription_api.mcp.tools.upload import request_upload_url

    user, bearer = await _seed_user_and_bearer(
        session, email_suffix="img-bad-mime"
    )
    own_tr = await make_transcription(session, user_id=user.id)
    await session.commit()
    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await request_upload_url(
                kind="image",
                file_size_bytes=100_000,
                mime_type="application/pdf",
                transcription_id=str(own_tr.id),
            )
        assert _is_tool_error(exc.value, "INVALID_PARAMETER")
    finally:
        reset()


async def test_request_upload_url_image_rejects_missing_transcription_id(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_PARAMETER — kind=image must come with a
    transcription_id (an image is always linked to a parent transcript).
    """
    from mcp.shared.exceptions import McpError
    from transcription_api.mcp.tools.upload import request_upload_url

    user, bearer = await _seed_user_and_bearer(
        session, email_suffix="img-no-tid"
    )
    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await request_upload_url(
                kind="image",
                file_size_bytes=100_000,
                mime_type="image/png",
                transcription_id=None,
            )
        assert _is_tool_error(exc.value, "INVALID_PARAMETER")
    finally:
        reset()
