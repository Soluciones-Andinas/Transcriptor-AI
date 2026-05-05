"""Per-user filesystem cache for transcription results.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-2  — Cache hit path (this file proves the read/write substrate; the
  end-to-end "cache_hit: true" assertion lives in the orchestrator + API
  tests of Batches 5 and 6).
- AC-12 — Two distinct users uploading the SAME audio file get TWO cache
  entries on disk in separate directories. There is NO global pool keyed
  only by audio_hash (D-027): the key is `(user_id, audio_hash)`.
- RF-CACHE-03 — Corrupt JSON on disk must NOT crash a read; the store
  returns ``None`` (cache miss) and lets the orchestrator regenerate the
  result. Logging is the responsibility of the caller; the store stays
  side-effect free except for filesystem writes.

The tests work entirely on tmp_path; they require neither GPU, ffmpeg,
Docker, nor a live database.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# AC-12 — per-user isolation
# ---------------------------------------------------------------------------
def test_cache_writes_and_reads_per_user(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-12 — Given user_a writes a payload under audio_hash H,
    When user_b reads under the same audio_hash H, Then the read returns
    None (no cross-user leak). user_a's read returns the original payload.
    """
    from transcription_api.pipeline.cache import CacheStore

    store = CacheStore(base_dir=tmp_path)
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    audio_hash = "deadbeef" * 8  # 64 chars hex

    payload = {"text": "Reunión A", "segments": [], "language": "es"}
    store.put(user_a, audio_hash, payload)

    assert store.get(user_a, audio_hash) == payload
    assert store.get(user_b, audio_hash) is None


def test_cache_isolates_users_via_filesystem_path(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-12 — Path layout is `<base>/<user_id>/<audio_hash>/result.json`.
    Two users share the audio_hash component but their directories are
    siblings, not the same. This makes the isolation visible/auditable on
    disk (operators can `ls` to see who has what).
    """
    from transcription_api.pipeline.cache import CacheStore

    store = CacheStore(base_dir=tmp_path)
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    audio_hash = "abc123ef" * 8

    store.put(user_a, audio_hash, {"text": "A"})

    path_a = tmp_path / str(user_a) / audio_hash / "result.json"
    path_b = tmp_path / str(user_b) / audio_hash / "result.json"
    assert path_a.exists()
    assert not path_b.exists()


# ---------------------------------------------------------------------------
# Generic semantics (miss, idempotency, delete)
# ---------------------------------------------------------------------------
def test_cache_returns_none_on_miss(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: ALT-1 — A miss returns None (not raise). The orchestrator
    branches on truthiness; raising would conflate misses with errors.
    """
    from transcription_api.pipeline.cache import CacheStore

    store = CacheStore(base_dir=tmp_path)
    assert store.get(uuid.uuid4(), "0" * 64) is None


def test_cache_put_overwrites_existing_entry(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: idempotent put — Two writes to the same (user_id, audio_hash)
    leave only the second payload. Useful when the orchestrator retries
    after a partially-written cache (e.g., crash mid-write).
    """
    from transcription_api.pipeline.cache import CacheStore

    store = CacheStore(base_dir=tmp_path)
    uid = uuid.uuid4()
    h = "f" * 64
    store.put(uid, h, {"text": "first"})
    store.put(uid, h, {"text": "second"})
    assert store.get(uid, h) == {"text": "second"}


def test_cache_delete_removes_entry_and_is_idempotent(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: cleanup support — delete removes the entry and is a no-op
    on a missing entry (idempotent so the cleanup task in Batch 7 can run
    repeatedly without race-conditioned failures).
    """
    from transcription_api.pipeline.cache import CacheStore

    store = CacheStore(base_dir=tmp_path)
    uid = uuid.uuid4()
    h = "9" * 64
    store.put(uid, h, {"text": "x"})
    assert store.get(uid, h) == {"text": "x"}

    store.delete(uid, h)
    assert store.get(uid, h) is None
    # Idempotent: second delete must not raise.
    store.delete(uid, h)


# ---------------------------------------------------------------------------
# RF-CACHE-03 — graceful handling of corrupt cache files
# ---------------------------------------------------------------------------
def test_cache_get_returns_none_when_file_is_corrupt_json(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: RF-CACHE-03 — Given a result.json on disk whose contents
    are not valid JSON (truncated write, manual edit), When get() is
    invoked, Then it returns None so the orchestrator can fall through to
    a fresh pipeline run rather than 500-ing on the cache layer.
    """
    from transcription_api.pipeline.cache import CacheStore

    uid = uuid.uuid4()
    h = "c0ffee" * 10 + "abcd"  # 64 chars
    bad_dir = tmp_path / str(uid) / h
    bad_dir.mkdir(parents=True)
    (bad_dir / "result.json").write_text("{ this is not json")

    store = CacheStore(base_dir=tmp_path)
    assert store.get(uid, h) is None


# ---------------------------------------------------------------------------
# Defensive: invalid audio_hash should not allow path traversal
# ---------------------------------------------------------------------------
def test_cache_rejects_path_traversal_in_audio_hash(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: defense-in-depth — audio_hash comes from a SHA-256 hex
    digest produced by normalize_audio, so it is always 64 lowercase hex
    chars. The cache must refuse any input that is not, so that a future
    bug elsewhere can't cause a write to ``../../etc/passwd``.
    """
    from transcription_api.pipeline.cache import CacheStore

    store = CacheStore(base_dir=tmp_path)
    uid = uuid.uuid4()
    with pytest.raises(ValueError):
        store.put(uid, "../../etc/passwd", {"text": "x"})
    with pytest.raises(ValueError):
        store.get(uid, "..")
