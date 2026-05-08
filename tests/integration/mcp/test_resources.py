"""MCP resources — RF-MCP-06 + RF-IMG.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 5 task 5.1):
- AC-6 — ``transcription://{id}`` returns the same dict shape as
  ``get_transcription(id)`` (the resource is a thin reuse-wrapper).
  Cross-user / unknown / soft-deleted all collapse to NOT_FOUND.
- AC-7 — ``transcription://{tid}/images/{iid}`` returns the binary
  bytes of the image file. Cross-user / unknown image / soft-deleted
  / image-not-attached-to-tid -> ``IMAGE_NOT_FOUND``.

Tests run unit-style: import the resource handler functions
directly, arm ContextVars, hit the testcontainer Postgres + the
filesystem (image bytes). ``requires_docker``.
"""
from __future__ import annotations

import secrets
from uuid import UUID, uuid4

import pytest

from tests.factories import (
    make_bearer,
    make_image,
    make_transcription,
    make_user,
)
from transcription_api.auth.mcp_bearer import generate_bearer

pytestmark = pytest.mark.requires_docker


# ---------------------------------------------------------------------------
# Helpers
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


def _stage_image_bytes(tmp_data_dir, *, user_id, transcription_id, image_id, payload: bytes):
    """Drop the image file at the canonical blobs path so the resource
    can read it. Returns the absolute path."""
    blobs = (
        tmp_data_dir
        / "blobs"
        / str(user_id)
        / str(transcription_id)
    )
    blobs.mkdir(parents=True, exist_ok=True)
    path = blobs / f"{image_id}.png"
    path.write_bytes(payload)
    return path


# ---------------------------------------------------------------------------
# AC-6 — transcription://{id} resource
# ---------------------------------------------------------------------------
async def test_transcription_resource_returns_full_payload(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-6 — transcription://{id} returns the same dict
    shape as get_transcription. Reuse helper keeps the contract in
    one place.
    """
    from transcription_api.mcp.resources import transcription_resource

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"res-{secrets.token_hex(2)}"
    )
    row = await make_transcription(
        session, user_id=user.id, audio_hash="res-" + "0" * 60
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        result = await transcription_resource(transcription_id=str(row.id))
    finally:
        reset()

    assert result["id"] == str(row.id)
    assert result["audio_hash"] == row.audio_hash
    assert "segments" in result
    assert "images" in result


async def test_transcription_resource_cross_user_returns_not_found(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-6 — Cross-user resource read collapses to
    TRANSCRIPTION_NOT_FOUND (delegation to get_transcription preserves
    the no-existence-leak invariant).
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.resources import transcription_resource

    alice, _ = await _seed_user_with_bearer(
        session, email_suffix=f"alice-{secrets.token_hex(2)}"
    )
    alice_row = await make_transcription(
        session, user_id=alice.id, audio_hash="alice-" + "0" * 58
    )
    bob, bob_bearer = await _seed_user_with_bearer(
        session, email_suffix=f"bob-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset = _arm_context(bob.id, bob_bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await transcription_resource(transcription_id=str(alice_row.id))
        assert _is_tool_error(exc.value, "TRANSCRIPTION_NOT_FOUND")
    finally:
        reset()


# ---------------------------------------------------------------------------
# AC-7 — transcription://{tid}/images/{iid} resource
# ---------------------------------------------------------------------------
async def test_image_resource_returns_bytes_for_owned_image(
    session, monkeypatch, tmp_path
):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-7 — Owner reads an attached image -> the resource
    handler returns the file bytes verbatim.
    """
    monkeypatch.setattr(
        "transcription_api.config.settings.data_dir", tmp_path
    )
    from transcription_api.mcp.resources import image_resource

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"img-{secrets.token_hex(2)}"
    )
    row = await make_transcription(
        session, user_id=user.id, audio_hash="iown-" + "0" * 59
    )
    img = await make_image(
        session,
        transcription_id=row.id,
        user_id=user.id,
    )
    payload = b"\x89PNG fake-image-bytes"
    # Persist the file at the file_path the factory wrote into the row.
    # The factory uses 'blobs/<random>.png' which is NOT under tmp_path —
    # update the row to a path we control.
    canonical_path = _stage_image_bytes(
        tmp_path,
        user_id=user.id,
        transcription_id=row.id,
        image_id=img.id,
        payload=payload,
    )
    img.file_path = str(canonical_path)
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        result = await image_resource(
            transcription_id=str(row.id),
            image_id=str(img.id),
        )
    finally:
        reset()

    assert isinstance(result, bytes), f"expected bytes; got {type(result)}"
    assert result == payload


async def test_image_resource_cross_user_returns_image_not_found(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-7 + ADR-014/015 — Bob requests Alice's image ->
    IMAGE_NOT_FOUND (listener filters Image.user_id; cross-user
    yields no row).
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.resources import image_resource

    alice, _ = await _seed_user_with_bearer(
        session, email_suffix=f"alice-{secrets.token_hex(2)}"
    )
    alice_row = await make_transcription(
        session, user_id=alice.id, audio_hash="alice-" + "0" * 58
    )
    alice_img = await make_image(
        session, transcription_id=alice_row.id, user_id=alice.id
    )
    bob, bob_bearer = await _seed_user_with_bearer(
        session, email_suffix=f"bob-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset = _arm_context(bob.id, bob_bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await image_resource(
                transcription_id=str(alice_row.id),
                image_id=str(alice_img.id),
            )
        assert _is_tool_error(exc.value, "IMAGE_NOT_FOUND")
    finally:
        reset()


async def test_image_resource_unknown_image_returns_not_found(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-7 — Unknown image_id (not in DB) -> IMAGE_NOT_FOUND.
    Same body as cross-user — no existence leak.
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.resources import image_resource

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"ghost-{secrets.token_hex(2)}"
    )
    row = await make_transcription(
        session, user_id=user.id, audio_hash="ghost-" + "0" * 58
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await image_resource(
                transcription_id=str(row.id),
                image_id=str(uuid4()),
            )
        assert _is_tool_error(exc.value, "IMAGE_NOT_FOUND")
    finally:
        reset()


async def test_image_resource_invalid_uuid_returns_invalid_parameter(session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_PARAMETER — image_id or transcription_id
    not a parseable UUID.
    """
    from mcp.shared.exceptions import McpError

    from transcription_api.mcp.resources import image_resource

    user, bearer = await _seed_user_with_bearer(
        session, email_suffix=f"bad-{secrets.token_hex(2)}"
    )
    await session.commit()

    reset = _arm_context(user.id, bearer.id)
    try:
        with pytest.raises(McpError) as exc:
            await image_resource(
                transcription_id="not-a-uuid",
                image_id=str(uuid4()),
            )
        assert _is_tool_error(exc.value, "INVALID_PARAMETER")
    finally:
        reset()
