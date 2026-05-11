"""TTL purge of the per-user filesystem cache + upload session GC.

Spec: SPEC-capa3-pipeline-v1, SPEC-capa4-mcp-v1 (RF-CACHE-04)
Covers:
- AC-10 — Cache entries (``<base>/<user_id>/<audio_hash>/result.json``)
  whose ``mtime`` is older than ``ttl_seconds`` are unlinked.
- RF-CACHE-02 — Empty parent dirs (audio_hash, then user_id) are removed
  best-effort after their last file is purged so the disk layout reflects
  the post-purge state.
- RF-CACHE-04 (D-074, 2026-05-11) — Expired ``upload_sessions`` rows
  (``status IN ('requested','uploaded')`` with ``expires_at + grace_seconds < now``)
  are marked ``status='expired'`` and their on-disk uploads at
  ``<uploads_dir>/<upload_id>/`` are removed. Without this, a client
  that requests upload URLs and never POSTs the bytes leaks rows + (for
  the ``uploaded`` case) on-disk blobs until the container restarts.
- Concurrent-safety — Another worker may have unlinked a file between
  our ``getmtime`` and our ``unlink`` calls. ``FileNotFoundError`` and
  ``PermissionError`` are swallowed (logged at WARNING) so a benign race
  does not crash the cleanup task — which would in turn leave the cache
  to grow unbounded.

The wider lifespan integration lives in ``main.py`` (Batch 7 task 7.2):
``app.state.cleanup_task = asyncio.create_task(_cleanup_loop())`` runs
this function periodically and is cancelled on shutdown.
"""
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger("transcription_api.pipeline.cleanup")

# Result file name written by CacheStore.put — defined here too so the
# cleanup module is independent of CacheStore (no circular imports, no
# coupling beyond the shared on-disk layout).
_RESULT_FILENAME = "result.json"


def _safe_rmdir(path: Path) -> None:
    """Remove ``path`` if empty; swallow OSError (non-empty or missing)."""
    try:
        path.rmdir()
    except OSError:
        # Non-empty (other entries still live there) OR already gone.
        # Either way, the cleanup task is best-effort; don't crash.
        pass


def _is_expected_layout(result_file: Path, base_dir: Path) -> bool:
    """Return True if ``result_file`` matches ``<base>/<uid>/<hash>/result.json``.

    Defense-in-depth: a stray file inside the cache tree (e.g. an
    operator's manual ``touch base/result.json``) is NOT cleaned up — the
    purger is a TTL-driven layout-aware tool, not a generic janitor. The
    expected depth from base_dir is exactly 3 (user_id / audio_hash /
    result.json), so the file's parent.parent must equal base_dir.
    """
    return (
        result_file.name == _RESULT_FILENAME
        and result_file.parent.parent.parent == base_dir
    )


def purge_expired(base_dir: Path, ttl_seconds: int) -> int:
    """Walk the cache tree and unlink files older than ``ttl_seconds``.

    Returns the count of files actually deleted. Empty parent directories
    (audio_hash, then user_id) are removed afterwards via best-effort
    rmdir. Missing ``base_dir`` returns 0 silently (cold start case).
    """
    base_dir = Path(base_dir)
    if not base_dir.is_dir():
        return 0

    cutoff = time.time() - ttl_seconds
    deleted = 0

    # Materialize the candidate list BEFORE iterating: we mutate the tree
    # (rmdir on parents) inside the loop, and a live rglob iterator hits
    # ``FileNotFoundError`` when its scandir cursor lands on a directory
    # we just removed for an earlier file.
    candidates = list(base_dir.rglob(_RESULT_FILENAME))
    # The layout check below filters out stray files at unexpected depths.
    for result_file in candidates:
        if not _is_expected_layout(result_file, base_dir):
            continue
        try:
            mtime = result_file.stat().st_mtime
        except FileNotFoundError:
            continue  # raced by another worker; nothing to do
        except PermissionError:
            logger.warning(
                "cache_stat_denied path=%s error_id=CACHE_STAT_DENIED",
                result_file,
            )
            continue

        if mtime >= cutoff:
            continue  # within TTL; keep

        try:
            result_file.unlink()
        except FileNotFoundError:
            continue  # racy unlink by another worker
        except PermissionError:
            logger.warning(
                "cache_unlink_denied path=%s error_id=CACHE_UNLINK_DENIED",
                result_file,
            )
            continue

        deleted += 1
        # Cascade rmdir: audio_hash dir, then user_id dir. Both are
        # best-effort — non-empty dirs are kept (other entries still live
        # there), missing dirs are no-ops (already cleaned by another run).
        _safe_rmdir(result_file.parent)
        _safe_rmdir(result_file.parent.parent)

    if deleted:
        logger.info("cache_cleanup_purged count=%d base=%s", deleted, base_dir)

    return deleted


async def purge_expired_upload_sessions(
    session_factory: async_sessionmaker,
    uploads_dir: Path,
    grace_seconds: int,
) -> int:
    """Mark expired ``upload_sessions`` rows and remove their on-disk blobs.

    RF-CACHE-04 (D-074): a row in state ``requested`` (URL emitted, bytes
    never POSTed) or ``uploaded`` (bytes received, ``start_transcription``
    never called) past ``expires_at + grace_seconds`` is garbage. We
    transition it to ``expired`` and delete the matching
    ``<uploads_dir>/<upload_id>/`` directory.

    Order of operations is deliberate:
        1. SELECT candidates.
        2. For each: ``shutil.rmtree(<uploads_dir>/<id>/)`` best-effort.
        3. UPDATE rows to ``status='expired'`` in a single transaction.
        4. Commit.

    If we crash between (2) and (3) the next iteration retries: the
    filesystem is already clean (step 2 is idempotent), and the rows
    still match the SELECT predicate. We avoid the inverse order
    (UPDATE first, then rmtree) because that one leaves orphan files on
    disk if the rmtree fails after the row was marked expired.

    ``bypass_scoping`` is mandatory: the cleanup task has no
    ``session.info['user_id']`` armed (it runs globally for all users),
    so the ADR-015 fail-closed listener would otherwise raise
    ``ScopingNotArmedError`` on the SELECT.

    Returns the count of rows whose status was transitioned.
    """
    # Lazy imports to keep this module import-light for tooling that
    # doesn't have SQLAlchemy/asyncpg available (consistent with the
    # rest of pipeline/*).
    from sqlalchemy import select, update

    from ..db.models import UploadSession
    from ..db.scoping import bypass_scoping

    cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=grace_seconds)
    start_ts = time.monotonic()
    bytes_freed = 0
    expired_count = 0

    async with session_factory() as session:
        with bypass_scoping(session):
            stmt = select(
                UploadSession.id,
                UploadSession.user_id,
                UploadSession.kind,
            ).where(
                UploadSession.status.in_(("requested", "uploaded")),
                UploadSession.expires_at < cutoff,
            )
            rows = (await session.execute(stmt)).all()

            for row in rows:
                upload_dir = Path(uploads_dir) / str(row.id)
                if upload_dir.is_dir():
                    try:
                        bytes_freed += _dir_size(upload_dir)
                        shutil.rmtree(upload_dir)
                    except (FileNotFoundError, PermissionError) as exc:
                        logger.warning(
                            "upload_session_rmtree_failed "
                            "upload_id=%s reason=%s error_id=UPLOAD_RMTREE_FAILED",
                            row.id,
                            type(exc).__name__,
                        )
                logger.info(
                    "upload_session_expired upload_id=%s user_id=%s kind=%s",
                    row.id,
                    row.user_id,
                    row.kind,
                )

            if rows:
                update_stmt = (
                    update(UploadSession)
                    .where(UploadSession.id.in_([row.id for row in rows]))
                    .values(status="expired")
                )
                await session.execute(update_stmt)
                await session.commit()
                expired_count = len(rows)

    duration_ms = int((time.monotonic() - start_ts) * 1000)
    logger.info(
        "upload_session_cleanup_completed "
        "entries_purged=%d bytes_freed=%d duration_ms=%d",
        expired_count,
        bytes_freed,
        duration_ms,
    )
    return expired_count


def _dir_size(path: Path) -> int:
    """Sum the size of every file under ``path`` (one-level deep is the
    common case for upload sessions, but recurse safely just in case).

    Swallows errors per-file so a single permission glitch doesn't fail
    the whole purge cycle.
    """
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total
