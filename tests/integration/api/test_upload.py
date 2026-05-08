"""``POST /api/upload`` and ``POST /api/upload-image`` — RF-MCP-03 + RF-IMG.

Spec: SPEC-capa4-mcp-v1
Covers (Batch 2 task 2.4):
- AC-1 — Audio happy path: bearer matches the SHA-256 stored on the
  upload session, file streams to ``<DATA_DIR>/uploads/<upload_id>/original.bin``,
  status flips to ``'uploaded'``, ``uploaded_at`` is set.
- spec §4 ``MCP_BEARER_INVALID`` — wrong bearer (or missing header)
  returns 401.
- spec §4 ``FILE_TOO_LARGE`` — bytes streamed exceed
  ``expected_size_bytes * 1.05`` margin.
- AC-10 ``UPLOAD_SESSION_NOT_FOUND`` — nonce not found OR session
  ``expires_at < now()``. Same error code regardless of cause (no
  existence leak).
- spec §4 ``INVALID_PARAMETER`` — endpoint expects ``kind=audio`` but
  the session has ``kind=image`` (or vice versa).

Tests use FastAPI's app via httpx ASGITransport + a Postgres
testcontainer. The endpoint flow is fully self-contained (no MCP
SDK, no orchestrator); only the DB row + filesystem byte count
matter.
"""
from __future__ import annotations

import io
import secrets
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.factories import make_bearer, make_upload_session, make_user
from transcription_api.auth.mcp_bearer import generate_bearer

pytestmark = pytest.mark.requires_docker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def client(monkeypatch, tmp_path):
    """FastAPI app via ASGITransport. DATA_DIR pointed at tmp_path so the
    uploads land in an isolated tree per test."""
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


async def _seed(session, *, kind: str = "audio", expected_size: int = 1024):
    """Seed user + bearer + upload_session; return (plaintext, upload_row)."""
    user = await make_user(session, email=f"u-{secrets.token_hex(4)}@x")
    main_plain, main_hash = generate_bearer()
    bearer_row = await make_bearer(
        session, user_id=user.id, token_hash=main_hash
    )
    ephemeral_plain = secrets.token_urlsafe(32)
    ephemeral_hash = sha256(ephemeral_plain.encode("ascii")).hexdigest()
    upload = await make_upload_session(
        session,
        user_id=user.id,
        bearer_id=bearer_row.id,
        kind=kind,
        expected_size_bytes=expected_size,
        upload_bearer_hash=ephemeral_hash,
    )
    await session.commit()
    return ephemeral_plain, upload


# ---------------------------------------------------------------------------
# AC-1 — happy path
# ---------------------------------------------------------------------------
async def test_upload_audio_happy_returns_200_and_marks_uploaded(
    client, session, tmp_path
):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-1 — Given a valid bearer + nonce + audio file body
    smaller than expected_size_bytes, When POST /api/upload runs, Then
    200 with ``{"ok": True, "upload_id": ...}``, the file is on disk
    at ``DATA_DIR/uploads/<upload_id>/original.bin``, and the row's
    ``status`` flipped to ``'uploaded'`` with ``uploaded_at`` set.
    """
    from transcription_api.db.models import UploadSession

    plaintext, upload = await _seed(session, kind="audio", expected_size=1024)
    body = b"x" * 500  # well below expected*1.05

    resp = await client.post(
        f"/api/upload?session={upload.nonce}",
        files={"file": ("audio.bin", io.BytesIO(body), "audio/mpeg")},
        headers={"authorization": f"Bearer {plaintext}"},
    )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["upload_id"] == str(upload.id)

    # Filesystem invariant.
    expected_path = tmp_path / "uploads" / str(upload.id) / "original.bin"
    assert expected_path.exists(), f"missing {expected_path}"
    assert expected_path.read_bytes() == body

    # DB invariant — the endpoint committed via its own session.
    refreshed = (
        await session.execute(
            select(UploadSession).where(UploadSession.id == upload.id)
        )
    ).scalar_one()
    await session.refresh(refreshed)
    assert refreshed.status == "uploaded"
    assert refreshed.uploaded_at is not None


# ---------------------------------------------------------------------------
# Auth surface
# ---------------------------------------------------------------------------
async def test_upload_missing_bearer_returns_401(client, session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 MCP_BEARER_INVALID — no Authorization header.
    """
    plaintext, upload = await _seed(session)
    resp = await client.post(
        f"/api/upload?session={upload.nonce}",
        files={"file": ("a.bin", io.BytesIO(b"x"), "audio/mpeg")},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "MCP_BEARER_INVALID"


async def test_upload_wrong_bearer_returns_401(client, session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 MCP_BEARER_INVALID — bearer plaintext does NOT
    hash to the stored upload_bearer_hash.
    """
    _, upload = await _seed(session)
    resp = await client.post(
        f"/api/upload?session={upload.nonce}",
        files={"file": ("a.bin", io.BytesIO(b"x"), "audio/mpeg")},
        headers={"authorization": "Bearer this-is-not-the-issued-plaintext"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "MCP_BEARER_INVALID"


# ---------------------------------------------------------------------------
# Size cap
# ---------------------------------------------------------------------------
async def test_upload_oversize_returns_413_and_unlinks_partial(
    client, session, tmp_path
):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 FILE_TOO_LARGE — body bytes exceed
    ``expected_size_bytes * 1.05`` margin. The endpoint cleans the
    partial file before returning.
    """
    plaintext, upload = await _seed(session, kind="audio", expected_size=100)
    too_big_body = b"x" * 200  # 100 expected; 1.05*100 = 105 max

    resp = await client.post(
        f"/api/upload?session={upload.nonce}",
        files={"file": ("a.bin", io.BytesIO(too_big_body), "audio/mpeg")},
        headers={"authorization": f"Bearer {plaintext}"},
    )

    assert resp.status_code == 413
    assert resp.json()["detail"]["error_code"] == "FILE_TOO_LARGE"
    leftover = tmp_path / "uploads" / str(upload.id) / "original.bin"
    assert not leftover.exists(), "partial upload must be unlinked"


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------
async def test_upload_unknown_nonce_returns_404(client, session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-10 UPLOAD_SESSION_NOT_FOUND — nonce does not match
    any row.
    """
    plaintext, _ = await _seed(session)
    resp = await client.post(
        "/api/upload?session=totally_unknown_nonce_value",
        files={"file": ("a.bin", io.BytesIO(b"x"), "audio/mpeg")},
        headers={"authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "UPLOAD_SESSION_NOT_FOUND"


async def test_upload_expired_session_returns_404(client, session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-10 UPLOAD_SESSION_NOT_FOUND — session row exists but
    expires_at is in the past. SAME error code as unknown-nonce (no
    existence leak about whether the session ever existed).
    """
    from transcription_api.db.models import UploadSession

    plaintext, upload = await _seed(session)
    # Backdate expires_at well into the past.
    await session.execute(
        UploadSession.__table__.update()
        .where(UploadSession.id == upload.id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    )
    await session.commit()

    resp = await client.post(
        f"/api/upload?session={upload.nonce}",
        files={"file": ("a.bin", io.BytesIO(b"x"), "audio/mpeg")},
        headers={"authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "UPLOAD_SESSION_NOT_FOUND"


async def test_upload_kind_mismatch_returns_400(client, session):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: spec §4 INVALID_PARAMETER — POST /api/upload (audio
    endpoint) called with a session whose ``kind='image'``. The two
    endpoints are kept distinct so the per-kind storage layout is
    deterministic.
    """
    plaintext, upload = await _seed(session, kind="image")
    resp = await client.post(
        f"/api/upload?session={upload.nonce}",
        files={"file": ("a.bin", io.BytesIO(b"x"), "audio/mpeg")},
        headers={"authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "INVALID_PARAMETER"
