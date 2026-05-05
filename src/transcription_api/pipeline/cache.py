"""Per-user filesystem cache for transcription results.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-2  — Cache hit substrate. The orchestrator calls ``get`` before
  invoking the GPU pipeline; on hit it skips Whisper+pyannote and inserts
  a fresh row in ``transcriptions`` (the historical table grows, the
  compute does not repeat).
- AC-12 — Cache key is ``(user_id, audio_hash)`` (D-027). Two users
  uploading the same audio get TWO independent entries on disk under
  separate ``<user_id>/`` directories. Zero-leak by design.
- RF-CACHE-03 — Corrupt cache files are treated as a miss (return ``None``
  from ``get``) so the orchestrator can re-run rather than 500-ing on
  the cache layer. The store does not log; the caller decides if a
  warning is worth emitting.

Path layout::

    <base_dir>/<user_id>/<audio_hash>/result.json

``audio_hash`` is the 64-char lowercase hex SHA-256 produced by
``normalize_audio`` (Batch 2 task 2.1). The cache validates that contract
and refuses anything else (defense-in-depth against path-traversal).
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

# Lowercase hex SHA-256: exactly 64 chars from [0-9a-f]. Matches the output
# of ``hashlib.sha256(...).hexdigest()`` (the producer in normalize_audio).
_AUDIO_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_RESULT_FILENAME = "result.json"


def _validate_audio_hash(audio_hash: str) -> None:
    """Refuse anything that isn't a SHA-256 hex digest.

    Defense-in-depth: ``audio_hash`` flows from ``normalize_audio`` which
    always emits a digest. If a future bug ever lets a different value
    reach the cache, this check prevents a write or read on a path with
    ``..`` components or absolute prefixes.
    """
    if not _AUDIO_HASH_RE.fullmatch(audio_hash):
        raise ValueError(
            f"audio_hash must be 64 lowercase hex chars; got {audio_hash!r}"
        )


class CacheStore:
    """Persistent dictionary keyed by ``(user_id, audio_hash)``.

    Stores values as ``result.json`` files under
    ``<base_dir>/<user_id>/<audio_hash>/``. The store itself is stateless:
    two ``CacheStore(base_dir=...)`` instances over the same directory see
    each other's writes immediately.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)

    def _entry_path(self, user_id: uuid.UUID, audio_hash: str) -> Path:
        _validate_audio_hash(audio_hash)
        return self._base_dir / str(user_id) / audio_hash / _RESULT_FILENAME

    def get(self, user_id: uuid.UUID, audio_hash: str) -> dict[str, Any] | None:
        """Return the cached payload or ``None`` (miss / corrupt / absent)."""
        path = self._entry_path(user_id, audio_hash)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # RF-CACHE-03: a corrupt entry is observable to the caller as
            # a miss so the orchestrator can regenerate. Caller logs.
            return None

    def put(self, user_id: uuid.UUID, audio_hash: str, payload: dict[str, Any]) -> None:
        """Write ``payload`` as JSON, overwriting any existing entry.

        Uses an atomic rename so a concurrent reader never sees a partial
        write. The temp file lives in the same directory as the target so
        the rename stays on the same filesystem.
        """
        path = self._entry_path(user_id, audio_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)

    def delete(self, user_id: uuid.UUID, audio_hash: str) -> None:
        """Remove the entry; no-op if it isn't there.

        Idempotent so the cleanup task in Batch 7 can run repeatedly
        without race-conditioned failures (another worker may have just
        purged the same entry).
        """
        path = self._entry_path(user_id, audio_hash)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        # Best-effort: also remove the now-empty audio_hash directory so
        # ``ls`` of the user dir reflects the post-purge state. Leave the
        # user directory in place (other entries may live there).
        try:
            path.parent.rmdir()
        except OSError:
            pass
