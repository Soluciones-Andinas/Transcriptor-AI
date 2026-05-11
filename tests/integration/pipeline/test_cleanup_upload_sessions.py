"""``purge_expired_upload_sessions`` GCs expired upload_sessions + their blobs.

Spec: SPEC-capa4-mcp-v1, RF-CACHE-04
Drift: D-074 (2026-05-11) — closes the disk-leak gap where a client
that requests upload URLs but never POSTs the bytes leaves
``upload_sessions`` rows + on-disk blobs forever.

Covers:
- A ``requested`` session past ``expires_at + grace_seconds`` transitions
  to ``status='expired'`` and its ``<uploads_dir>/<id>/`` is removed.
- A ``uploaded`` session past the grace also transitions + its files
  (e.g. ``original.bin``) get rmtree'd.
- A ``requested`` session WITHIN the grace window stays untouched.
- A ``consumed`` session past expires_at stays untouched (it was already
  consumed by ``start_transcription``; the audit trail is preserved).
- A missing on-disk directory (row exists, files already gone) does NOT
  raise — the row is still transitioned to ``expired``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from tests.factories import make_bearer, make_upload_session, make_user
from transcription_api.db.models import UploadSession
from transcription_api.db.scoping import bypass_scoping
from transcription_api.pipeline.cleanup import purge_expired_upload_sessions

pytestmark = pytest.mark.requires_docker


async def test_purge_marks_expired_requested_and_removes_blob(
    session, tmp_path: Path
) -> None:
    """A ``requested`` session past grace transitions + blob rmtree'd."""
    user = await make_user(session, email="expired-req@x")
    plaintext_hash = "a" * 64
    bearer = await make_bearer(session, user_id=user.id, token_hash=plaintext_hash)
    upload = await make_upload_session(
        session,
        user_id=user.id,
        bearer_id=bearer.id,
        kind="audio",
        expires_at_offset_seconds=-600,  # expired 10 min ago
    )
    await session.commit()

    upload_dir = tmp_path / str(upload.id)
    upload_dir.mkdir()
    (upload_dir / "original.bin").write_bytes(b"x" * 1024)

    from transcription_api.db.session import async_session_factory

    expired_count = await purge_expired_upload_sessions(
        async_session_factory, tmp_path, grace_seconds=30
    )

    assert expired_count == 1
    assert not upload_dir.exists(), "blob directory must be removed"

    async with async_session_factory() as fresh:
        with bypass_scoping(fresh):
            refreshed = (
                await fresh.execute(
                    select(UploadSession).where(UploadSession.id == upload.id)
                )
            ).scalar_one()
            assert refreshed.status == "expired"


async def test_purge_marks_expired_uploaded_and_removes_blob(
    session, tmp_path: Path
) -> None:
    """An ``uploaded`` session past grace also transitions + blob rmtree'd."""
    user = await make_user(session, email="expired-upl@x")
    plaintext_hash = "b" * 64
    bearer = await make_bearer(session, user_id=user.id, token_hash=plaintext_hash)
    upload = await make_upload_session(
        session,
        user_id=user.id,
        bearer_id=bearer.id,
        kind="audio",
        expires_at_offset_seconds=-600,
    )
    upload.status = "uploaded"
    await session.flush()
    await session.commit()

    upload_dir = tmp_path / str(upload.id)
    upload_dir.mkdir()
    (upload_dir / "original.bin").write_bytes(b"y" * 2048)

    from transcription_api.db.session import async_session_factory

    expired_count = await purge_expired_upload_sessions(
        async_session_factory, tmp_path, grace_seconds=30
    )

    assert expired_count == 1
    assert not upload_dir.exists()


async def test_purge_skips_session_within_grace_window(
    session, tmp_path: Path
) -> None:
    """A ``requested`` session past expires_at but within grace stays put."""
    user = await make_user(session, email="grace@x")
    plaintext_hash = "c" * 64
    bearer = await make_bearer(session, user_id=user.id, token_hash=plaintext_hash)
    upload = await make_upload_session(
        session,
        user_id=user.id,
        bearer_id=bearer.id,
        kind="audio",
        # expired 10 sec ago — grace is 60 → still within window
        expires_at_offset_seconds=-10,
    )
    await session.commit()

    upload_dir = tmp_path / str(upload.id)
    upload_dir.mkdir()
    (upload_dir / "original.bin").write_bytes(b"z" * 512)

    from transcription_api.db.session import async_session_factory

    expired_count = await purge_expired_upload_sessions(
        async_session_factory, tmp_path, grace_seconds=60
    )

    assert expired_count == 0
    assert upload_dir.exists()


async def test_purge_skips_consumed_sessions(
    session, tmp_path: Path
) -> None:
    """A ``consumed`` session past expires_at is preserved (audit trail)."""
    user = await make_user(session, email="consumed@x")
    plaintext_hash = "d" * 64
    bearer = await make_bearer(session, user_id=user.id, token_hash=plaintext_hash)
    upload = await make_upload_session(
        session,
        user_id=user.id,
        bearer_id=bearer.id,
        kind="audio",
        expires_at_offset_seconds=-3600,
    )
    upload.status = "consumed"
    await session.flush()
    await session.commit()

    from transcription_api.db.session import async_session_factory

    expired_count = await purge_expired_upload_sessions(
        async_session_factory, tmp_path, grace_seconds=30
    )

    assert expired_count == 0
    async with async_session_factory() as fresh:
        with bypass_scoping(fresh):
            refreshed = (
                await fresh.execute(
                    select(UploadSession).where(UploadSession.id == upload.id)
                )
            ).scalar_one()
            assert refreshed.status == "consumed"


async def test_purge_tolerates_missing_blob_dir(
    session, tmp_path: Path
) -> None:
    """Row exists, but ``<uploads_dir>/<id>/`` is already gone → no error."""
    user = await make_user(session, email="missing-blob@x")
    plaintext_hash = "e" * 64
    bearer = await make_bearer(session, user_id=user.id, token_hash=plaintext_hash)
    upload = await make_upload_session(
        session,
        user_id=user.id,
        bearer_id=bearer.id,
        kind="audio",
        expires_at_offset_seconds=-1800,
    )
    await session.commit()

    # NOTE: no upload_dir created — simulates a previous cleanup iteration
    # that ran but crashed between rmtree and UPDATE (the retry path).

    from transcription_api.db.session import async_session_factory

    expired_count = await purge_expired_upload_sessions(
        async_session_factory, tmp_path, grace_seconds=30
    )

    assert expired_count == 1
    async with async_session_factory() as fresh:
        with bypass_scoping(fresh):
            refreshed = (
                await fresh.execute(
                    select(UploadSession).where(UploadSession.id == upload.id)
                )
            ).scalar_one()
            assert refreshed.status == "expired"
