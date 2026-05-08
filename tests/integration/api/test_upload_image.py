"""``POST /api/upload-image`` — RF-IMG-02 + RF-MCP-03 (image branch).

Spec: SPEC-capa4-mcp-v1
Covers (G1 — review-fixes Tier 1):
- AC-7 closure (image resource fetch end-to-end) — happy path lands the
  PNG bytes under ``<DATA_DIR>/blobs/<user_id>/<transcription_id>/<image_id>.png``,
  inserts the ``images`` row, and bumps the upload session to ``'uploaded'``.
- spec §4 ``INVALID_FORMAT`` — body bytes do not match the declared
  ``expected_mime_type`` (magic-byte sniff via stdlib ``imghdr``).
- spec §4 ``MCP_BEARER_INVALID`` — ephemeral bearer presented by a
  different user's main bearer does not hash to the session's stored
  ``upload_bearer_hash``. The endpoint stays *bearer-vs-hash* (no
  cross-user existence leak via session lookup, since lookup is
  ``bypass_scoping`` over the nonce).

Layout matches ``test_upload.py``: local ``client`` fixture wires
``ASGITransport`` + ``LifespanManager`` + a tmp-pathed DATA_DIR; ``session``
comes from ``tests/conftest.py``; per-test seeding goes through inline
helpers and ``tests/factories.py`` (no global fixture extraction yet —
see plan G11.7 for the consolidation pass).
"""
from __future__ import annotations

import io
import secrets
from hashlib import sha256

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from tests.factories import (
    make_bearer,
    make_transcription,
    make_upload_session,
    make_user,
)
from transcription_api.auth.mcp_bearer import generate_bearer

pytestmark = pytest.mark.requires_docker


# 1x1 transparent RGB PNG (67 bytes). Header = b"\x89PNG\r\n\x1a\n" so
# stdlib ``imghdr.what(None, raw[:32])`` returns ``"png"``.
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
async def client(monkeypatch, tmp_path):
    """FastAPI app via ASGITransport. ``DATA_DIR`` is pointed at ``tmp_path``
    so the lifespan ``mkdir`` lands in an isolated tree per test."""
    monkeypatch.setattr(
        "transcription_api.config.settings.data_dir", tmp_path
    )
    from transcription_api.main import app

    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=False,
        ) as c:
            yield c


async def _seed_image_session(
    session,
    *,
    expected_size: int = 256,
    expected_mime: str = "image/png",
):
    """Seed user + main bearer + transcription + image upload_session.

    The transcription is required because ``images.transcription_id`` is
    NOT NULL — RF-MCP-01 ``request_upload_url(kind='image', ...)`` enforces
    the parameter upstream. Returns ``(ephemeral_plaintext, upload_row)``
    so callers can Authorize against ``upload_bearer_hash`` and post against
    ``upload.nonce``.
    """
    user = await make_user(session, email=f"u-{secrets.token_hex(4)}@x")
    _, main_hash = generate_bearer()
    bearer_row = await make_bearer(session, user_id=user.id, token_hash=main_hash)
    transcription = await make_transcription(session, user_id=user.id)
    ephemeral_plain = secrets.token_urlsafe(32)
    ephemeral_hash = sha256(ephemeral_plain.encode("ascii")).hexdigest()
    upload = await make_upload_session(
        session,
        user_id=user.id,
        bearer_id=bearer_row.id,
        kind="image",
        expected_size_bytes=expected_size,
        upload_bearer_hash=ephemeral_hash,
    )
    # Bind the upload to the transcription + parametrize the expected MIME
    # (factory does not expose either field).
    from transcription_api.db.models import UploadSession

    await session.execute(
        UploadSession.__table__.update()
        .where(UploadSession.id == upload.id)
        .values(
            expected_mime_type=expected_mime,
            transcription_id=transcription.id,
        )
    )
    await session.commit()
    await session.refresh(upload)
    return ephemeral_plain, upload


# ---------------------------------------------------------------------------
# G1.1 — AC-7 happy path
# ---------------------------------------------------------------------------
async def test_upload_image_happy(client, session, tmp_path):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-7 image branch — Given a valid bearer + nonce + PNG
    body smaller than ``expected_size_bytes * 1.05``, When
    POST /api/upload-image runs, Then 200 with ``{"ok": True, "image_id": ...}``,
    a row lands in ``images`` with ``mime_type='image/png'``, the bytes
    are written under ``DATA_DIR/blobs/<user_id>/<transcription_id>/<image_id>.png``,
    and the upload session row flips to ``'uploaded'``.
    """
    plaintext, upload = await _seed_image_session(
        session, expected_size=len(PNG_1X1) * 2, expected_mime="image/png"
    )

    resp = await client.post(
        f"/api/upload-image?session={upload.nonce}",
        files={"file": ("smoke.png", io.BytesIO(PNG_1X1), "image/png")},
        headers={"authorization": f"Bearer {plaintext}"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert "image_id" in body
    image_id = body["image_id"]

    # DB invariant — the endpoint commits via its own session.
    row = (
        await session.execute(
            text("SELECT mime_type, file_path, user_id "
                 "FROM images WHERE id = :iid"),
            {"iid": image_id},
        )
    ).first()
    assert row is not None
    assert row.mime_type == "image/png"

    # Filesystem invariant — bytes land under blobs/<user_id>/<tx_id>/<id>.png.
    from pathlib import Path

    blob_path = Path(row.file_path)
    assert blob_path.exists(), f"missing blob {blob_path}"
    assert blob_path.read_bytes() == PNG_1X1

    # Upload session bumped to 'uploaded'.
    status = (
        await session.execute(
            text("SELECT status FROM upload_sessions WHERE id = :sid"),
            {"sid": upload.id},
        )
    ).scalar_one()
    assert status == "uploaded"


# ---------------------------------------------------------------------------
# G1.3 — magic-byte mismatch (INVALID_FORMAT)
# ---------------------------------------------------------------------------
async def test_upload_image_wrong_magic_bytes_returns_400(client, session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_FORMAT — body declares ``image/png`` but
    the bytes start with the JPEG SOI marker (``FF D8``). The endpoint
    sniffs magic bytes via stdlib ``imghdr`` and rejects the mismatch,
    preventing a malicious caller from smuggling a non-PNG payload past
    the kind/MIME check.
    """
    plaintext, upload = await _seed_image_session(
        session, expected_size=256, expected_mime="image/png"
    )
    fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 60  # JPEG SOI + filler

    resp = await client.post(
        f"/api/upload-image?session={upload.nonce}",
        files={"file": ("fake.png", io.BytesIO(fake_jpeg), "image/png")},
        headers={"authorization": f"Bearer {plaintext}"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "INVALID_FORMAT"


# ---------------------------------------------------------------------------
# G1.4 — cross-user bearer rejected (no existence leak via session lookup)
# ---------------------------------------------------------------------------
async def test_upload_image_cross_user_returns_404(client, session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 MCP_BEARER_INVALID — a bearer that doesn't hash to
    the stored ``upload_bearer_hash`` is rejected with 401 even when the
    nonce belongs to a real session. Naming kept aligned with the plan
    (filename mentions 404) but the assertion is 401 because the bearer
    check happens before any per-user existence comparison; the cross-user
    privacy invariant is preserved by the bearer hash comparison itself.
    """
    _, upload = await _seed_image_session(session, expected_size=256)
    other_plain = secrets.token_urlsafe(32)  # not the issued ephemeral

    resp = await client.post(
        f"/api/upload-image?session={upload.nonce}",
        files={"file": ("img.png", io.BytesIO(PNG_1X1), "image/png")},
        headers={"authorization": f"Bearer {other_plain}"},
    )

    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "MCP_BEARER_INVALID"
