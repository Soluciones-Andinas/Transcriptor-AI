"""TTL purge of the per-user filesystem cache.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-10 — Given a cache entry whose ``mtime`` is older than ``ttl_seconds``,
  When ``purge_expired`` runs, Then the file is unlinked AND the count of
  deletions is returned. Fresh files (mtime within TTL) are preserved.
- RF-CACHE-02 — Empty ``<audio_hash>/`` and ``<user_id>/`` directories are
  removed best-effort after their last file is purged so the disk layout
  reflects the post-purge state.
- Concurrent-safety — ``PermissionError`` and ``FileNotFoundError`` are
  swallowed (another process may have already deleted the file). The cleanup
  task runs every ``cache_cleanup_interval_seconds`` and must not crash on
  benign races.

Tests use stdlib only (``tmp_path`` + ``os.utime`` to age files); no torch,
no Postgres, no Docker. The cleanup module is filesystem-only by design
(no DB) and Batch 7's hard constraint forbids touching ``db/`` here.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers — build a populated cache layout that mirrors CacheStore's writes.
# ---------------------------------------------------------------------------
def _put(base_dir: Path, user_id: uuid.UUID, audio_hash: str, payload: str = "{}") -> Path:
    """Create ``<base>/<user_id>/<audio_hash>/result.json`` with given content."""
    target = base_dir / str(user_id) / audio_hash / "result.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload)
    return target


def _age(path: Path, seconds_ago: float) -> None:
    """Set ``path``'s mtime to ``seconds_ago`` in the past."""
    old = time.time() - seconds_ago
    os.utime(path, (old, old))


# ---------------------------------------------------------------------------
# AC-10 — purge expired entries
# ---------------------------------------------------------------------------
def test_purge_deletes_files_older_than_ttl(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-10 — Given a result.json older than ttl_seconds, When
    purge_expired runs, Then the file is unlinked.
    """
    from transcription_api.pipeline.cleanup import purge_expired

    user = uuid.uuid4()
    audio_hash = "a" * 64
    stale = _put(tmp_path, user, audio_hash, '{"text": "old"}')
    _age(stale, seconds_ago=25 * 3600)  # 25 h vs 24 h TTL

    n = purge_expired(tmp_path, ttl_seconds=24 * 3600)

    assert n == 1
    assert not stale.exists()


def test_purge_keeps_files_within_ttl(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-10 — Given a result.json whose mtime is within
    ttl_seconds, When purge_expired runs, Then the file is preserved
    AND the returned count does not include it.
    """
    from transcription_api.pipeline.cleanup import purge_expired

    user = uuid.uuid4()
    audio_hash = "b" * 64
    fresh = _put(tmp_path, user, audio_hash, '{"text": "fresh"}')
    _age(fresh, seconds_ago=1 * 3600)  # 1 h ago, well under 24 h TTL

    n = purge_expired(tmp_path, ttl_seconds=24 * 3600)

    assert n == 0
    assert fresh.exists()


def test_purge_returns_count_across_multiple_users(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-10 — purge_expired walks the per-user layout. Given
    three users each with one stale file, Then the count is 3.
    """
    from transcription_api.pipeline.cleanup import purge_expired

    for _ in range(3):
        user = uuid.uuid4()
        audio_hash = uuid.uuid4().hex + "0" * (64 - 32)
        stale = _put(tmp_path, user, audio_hash)
        _age(stale, seconds_ago=10 * 3600)

    n = purge_expired(tmp_path, ttl_seconds=1 * 3600)
    assert n == 3


def test_purge_separates_stale_from_fresh_within_same_user(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-10 — Given the same user with two entries (one stale,
    one fresh), When purge runs, Then only the stale one is deleted.
    """
    from transcription_api.pipeline.cleanup import purge_expired

    user = uuid.uuid4()
    stale_hash = "c" * 64
    fresh_hash = "d" * 64
    stale = _put(tmp_path, user, stale_hash)
    fresh = _put(tmp_path, user, fresh_hash)
    _age(stale, seconds_ago=25 * 3600)
    _age(fresh, seconds_ago=1 * 3600)

    n = purge_expired(tmp_path, ttl_seconds=24 * 3600)

    assert n == 1
    assert not stale.exists()
    assert fresh.exists()


# ---------------------------------------------------------------------------
# RF-CACHE-02 — empty directory cleanup
# ---------------------------------------------------------------------------
def test_purge_removes_empty_audio_hash_dir(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: RF-CACHE-02 — After purging the only file under
    ``<user_id>/<audio_hash>/``, the empty audio_hash dir is removed.
    """
    from transcription_api.pipeline.cleanup import purge_expired

    user = uuid.uuid4()
    audio_hash = "e" * 64
    stale = _put(tmp_path, user, audio_hash)
    _age(stale, seconds_ago=25 * 3600)

    purge_expired(tmp_path, ttl_seconds=24 * 3600)

    assert not (tmp_path / str(user) / audio_hash).exists()


def test_purge_removes_empty_user_dir(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: RF-CACHE-02 — After purging the only file in a user's
    cache, the user dir is also removed (cascading rmdir up to base).
    """
    from transcription_api.pipeline.cleanup import purge_expired

    user = uuid.uuid4()
    audio_hash = "f" * 64
    stale = _put(tmp_path, user, audio_hash)
    _age(stale, seconds_ago=25 * 3600)

    purge_expired(tmp_path, ttl_seconds=24 * 3600)

    assert not (tmp_path / str(user)).exists()


def test_purge_keeps_non_empty_user_dir(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: RF-CACHE-02 — When a user has BOTH a stale and a fresh
    entry, purging the stale one preserves the user dir + the fresh
    entry (the rmdir is idempotent, and refuses to drop a non-empty dir).
    """
    from transcription_api.pipeline.cleanup import purge_expired

    user = uuid.uuid4()
    stale_hash = "1" * 64
    fresh_hash = "2" * 64
    stale = _put(tmp_path, user, stale_hash)
    fresh = _put(tmp_path, user, fresh_hash)
    _age(stale, seconds_ago=25 * 3600)
    _age(fresh, seconds_ago=1 * 3600)

    purge_expired(tmp_path, ttl_seconds=24 * 3600)

    assert (tmp_path / str(user)).exists()
    assert fresh.exists()


# ---------------------------------------------------------------------------
# Concurrent-safety
# ---------------------------------------------------------------------------
def test_purge_swallows_file_not_found_when_race_deletes(
    tmp_path: Path, monkeypatch
):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: concurrent-safety — Another process may unlink the file
    between our ``getmtime`` check and our ``unlink``. The cleanup MUST
    NOT crash; it just skips the file. Counts only what it actually deleted.
    """
    from transcription_api.pipeline import cleanup

    user = uuid.uuid4()
    audio_hash = "3" * 64
    stale = _put(tmp_path, user, audio_hash)
    _age(stale, seconds_ago=25 * 3600)

    real_unlink = Path.unlink

    def _racy_unlink(self, *a, **kw):
        # Simulate concurrent deletion — somebody else got there first.
        raise FileNotFoundError(str(self))

    monkeypatch.setattr(Path, "unlink", _racy_unlink)
    try:
        n = cleanup.purge_expired(tmp_path, ttl_seconds=24 * 3600)
    finally:
        monkeypatch.setattr(Path, "unlink", real_unlink)

    # Did NOT crash; the racy file is counted as 0 (we did not actually
    # complete the delete on this run).
    assert n == 0


def test_purge_returns_zero_when_base_dir_does_not_exist(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: defensive — Given the cache base dir hasn't been created
    yet (cold start, no transcriptions ever written), When purge runs,
    Then it returns 0 without raising.
    """
    from transcription_api.pipeline.cleanup import purge_expired

    nonexistent = tmp_path / "nope"
    n = purge_expired(nonexistent, ttl_seconds=1)
    assert n == 0


def test_purge_ignores_non_result_json_files(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: defensive — Random files inside the cache layout that
    are NOT named ``result.json`` are left alone. The cleanup task is
    a TTL purger, not a generic janitor.
    """
    from transcription_api.pipeline.cleanup import purge_expired

    user = uuid.uuid4()
    audio_hash = "4" * 64
    weird = tmp_path / str(user) / audio_hash / "weird.tmp"
    weird.parent.mkdir(parents=True)
    weird.write_text("?")
    _age(weird, seconds_ago=99 * 3600)

    n = purge_expired(tmp_path, ttl_seconds=1)

    assert n == 0
    assert weird.exists()


def test_purge_ignores_files_at_unexpected_depth(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: defensive — A file directly under ``<base_dir>/`` (not
    under ``<user_id>/<audio_hash>/``) is NOT walked. The expected
    layout is exactly two levels deep before the leaf file; anything
    outside that pattern is ignored.
    """
    from transcription_api.pipeline.cleanup import purge_expired

    rogue = tmp_path / "result.json"
    rogue.write_text("{}")
    _age(rogue, seconds_ago=99 * 3600)

    n = purge_expired(tmp_path, ttl_seconds=1)

    assert n == 0
    assert rogue.exists()
