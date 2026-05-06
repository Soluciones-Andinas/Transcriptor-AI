"""Unit tests for lifespan helpers — CR-4 model release + H-4 orphan purge.

Spec: SPEC-capa3-pipeline-v1
Capa 3 review:
- CR-4 — `_release_models` drops CUDA-bound references and triggers
  torch.cuda.empty_cache() when available. Tested with a stub app.state
  + monkey-patched torch module.
- H-4 — `_purge_orphan_uploads` deletes every file in uploads_dir at
  startup. Tested with tmp_path.

Pure-Python unit tests — no Postgres / asyncio / FastAPI lifespan needed.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_fake_app(**state_attrs):
    """Build a stub FastAPI-like object with `app.state` attributes."""
    state = SimpleNamespace(**state_attrs)
    return SimpleNamespace(state=state)


# ---------------------------------------------------------------------------
# H-4 — _purge_orphan_uploads
# ---------------------------------------------------------------------------
def test_purge_orphan_uploads_removes_files(tmp_path, monkeypatch):
    """Every file in uploads_dir is deleted on startup (H-4)."""
    from transcription_api import main as _main

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "a.mp3").write_bytes(b"x")
    (uploads / "b.wav").write_bytes(b"y")
    (uploads / "subdir").mkdir()  # subdirs left alone

    fake_settings = SimpleNamespace(uploads_dir=uploads)
    monkeypatch.setattr(_main, "settings", fake_settings)

    _main._purge_orphan_uploads()

    assert not (uploads / "a.mp3").exists()
    assert not (uploads / "b.wav").exists()
    assert (uploads / "subdir").is_dir()  # subdirs untouched


def test_purge_orphan_uploads_handles_missing_dir(tmp_path, monkeypatch):
    """No-op (silent) if uploads_dir doesn't exist."""
    from transcription_api import main as _main

    fake_settings = SimpleNamespace(uploads_dir=tmp_path / "does-not-exist")
    monkeypatch.setattr(_main, "settings", fake_settings)

    # Must not raise.
    _main._purge_orphan_uploads()


def test_purge_orphan_uploads_logs_unlink_failure(
    tmp_path, monkeypatch, caplog
):
    """If unlink fails, log + continue (best-effort)."""
    from transcription_api import main as _main

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    blocked = uploads / "stuck.mp3"
    blocked.write_bytes(b"x")

    # Patch Path.unlink to simulate OSError on this specific file.
    original_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name == "stuck.mp3":
            raise OSError("simulated EPERM")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    fake_settings = SimpleNamespace(uploads_dir=uploads)
    monkeypatch.setattr(_main, "settings", fake_settings)

    with caplog.at_level("WARNING", logger="transcription_api"):
        _main._purge_orphan_uploads()

    assert blocked.exists()  # not deleted
    assert any("UPLOAD_ORPHAN_LEAK" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# CR-4 — _release_models
# ---------------------------------------------------------------------------
def test_release_models_clears_state_attrs():
    """Whisper + pyannote refs set to None so GC can reclaim."""
    from transcription_api import main as _main

    app = _make_fake_app(
        whisper_model=MagicMock(),
        pyannote_pipeline=MagicMock(),
    )
    _main._release_models(app)

    assert app.state.whisper_model is None
    assert app.state.pyannote_pipeline is None


def test_release_models_skips_when_attrs_already_none():
    """No-op when the model attrs are already None / absent."""
    from transcription_api import main as _main

    app = _make_fake_app(whisper_model=None, pyannote_pipeline=None)
    # Must not raise.
    _main._release_models(app)
    assert app.state.whisper_model is None
    assert app.state.pyannote_pipeline is None


def test_release_models_calls_torch_cuda_empty_cache_when_available(monkeypatch):
    """If torch is importable AND cuda.is_available(), empty_cache fires."""
    from transcription_api import main as _main

    fake_cuda = MagicMock()
    fake_cuda.is_available.return_value = True
    fake_cuda.empty_cache = MagicMock()

    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = fake_cuda  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    app = _make_fake_app(whisper_model=MagicMock(), pyannote_pipeline=MagicMock())
    _main._release_models(app)

    fake_cuda.empty_cache.assert_called_once()


def test_release_models_silent_when_torch_unavailable(monkeypatch):
    """ImportError on `import torch` is swallowed (Capa 1+2 image has no torch)."""
    from transcription_api import main as _main

    # Force `import torch` to fail.
    monkeypatch.setitem(sys.modules, "torch", None)

    app = _make_fake_app(whisper_model=MagicMock(), pyannote_pipeline=MagicMock())
    # Must not raise.
    _main._release_models(app)


def test_release_models_silent_when_cuda_not_available(monkeypatch):
    """If torch is present but cuda is unavailable, empty_cache is NOT called."""
    from transcription_api import main as _main

    fake_cuda = MagicMock()
    fake_cuda.is_available.return_value = False
    fake_cuda.empty_cache = MagicMock()

    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = fake_cuda  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    app = _make_fake_app(whisper_model=MagicMock(), pyannote_pipeline=MagicMock())
    _main._release_models(app)

    fake_cuda.empty_cache.assert_not_called()


# ---------------------------------------------------------------------------
# CR-3 — streaming SHA-256 / bounded magic-byte read in normalize
# ---------------------------------------------------------------------------
def test_streaming_sha256_matches_full_file_hash(tmp_path):
    """Chunked SHA-256 produces the same digest as the legacy whole-file hash."""
    import hashlib

    from transcription_api.pipeline.normalize import _sha256_file

    blob = b"\x00" * (256 * 1024) + b"abc" + b"\xff" * (32 * 1024)
    target = tmp_path / "blob.bin"
    target.write_bytes(blob)

    expected = hashlib.sha256(blob).hexdigest()
    actual = _sha256_file(target)
    assert actual == expected


def test_streaming_sha256_handles_small_file(tmp_path):
    """A file smaller than the chunk size still hashes correctly."""
    import hashlib

    from transcription_api.pipeline.normalize import _sha256_file

    target = tmp_path / "tiny.bin"
    target.write_bytes(b"hello")
    assert _sha256_file(target) == hashlib.sha256(b"hello").hexdigest()


def test_read_magic_head_bounded_to_64_bytes(tmp_path):
    """Even if the file is huge, only 64 bytes are loaded into RAM."""
    from transcription_api.pipeline.normalize import _MAGIC_HEAD_BYTES, _read_magic_head

    big = tmp_path / "huge.bin"
    big.write_bytes(b"X" * (10 * 1024 * 1024))  # 10 MB
    head = _read_magic_head(big)
    assert head == b"X" * _MAGIC_HEAD_BYTES
    assert len(head) == _MAGIC_HEAD_BYTES
