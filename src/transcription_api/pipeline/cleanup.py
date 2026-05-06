"""TTL purge of the per-user filesystem cache.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-10 — Cache entries (``<base>/<user_id>/<audio_hash>/result.json``)
  whose ``mtime`` is older than ``ttl_seconds`` are unlinked.
- RF-CACHE-02 — Empty parent dirs (audio_hash, then user_id) are removed
  best-effort after their last file is purged so the disk layout reflects
  the post-purge state.
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
import time
from pathlib import Path

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
