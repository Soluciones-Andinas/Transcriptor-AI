"""Audio normalization to 16 kHz mono WAV with content-derived hash.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-1 — `normalize_audio` returns a 16 kHz mono WAV path, a 64-char hex
  SHA-256 of the normalized bytes, and the audio duration (used as
  `metadata.duration_seconds` in the response and as the cache key
  along with user_id).
- AC-4 — Inputs whose extension or magic bytes don't match the
  whitelist (mp4, mp3, m4a, wav, flac) are rejected with
  `AudioFormatInvalid` BEFORE shelling out to ffmpeg, so the API can
  produce a 400 with a stable reason without spawning a subprocess.

Tests that actually call ffmpeg/ffprobe are marked ``requires_ffmpeg`` and
auto-skip on machines without those binaries (handled in conftest).
Validation-only tests (extension/magic) need neither binary and run
unconditionally.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers (no-ffmpeg variant only)
# ---------------------------------------------------------------------------
def _write_with_ext(tmp_path: Path, name: str, payload: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(payload)
    return p


@pytest.fixture
def sine_mp3(tmp_path: Path) -> Path:
    """Generate a 1-second 440 Hz sine MP3 via ffmpeg.

    Used by tests marked ``requires_ffmpeg``. Failures here propagate so a
    misconfigured PATH is not silently masked as a successful skip.
    """
    out = tmp_path / "sine.mp3"
    subprocess.run(  # noqa: S603,S607
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            str(out),
        ],
        check=True,
    )
    return out


# ---------------------------------------------------------------------------
# AC-4 — extension and magic-byte validation (ffmpeg not invoked)
# ---------------------------------------------------------------------------
def test_normalize_rejects_extension_outside_whitelist(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-4 — Given an input with an extension not in the whitelist
    (mp4/mp3/m4a/wav/flac), When normalize_audio is invoked, Then
    AudioFormatInvalid is raised before ffmpeg is touched and the reason
    cites the offending extension.
    """
    from transcription_api.pipeline.normalize import (
        AudioFormatInvalid,
        normalize_audio,
    )

    bad = _write_with_ext(tmp_path, "fake.exe", b"MZ\x90\x00")
    with pytest.raises(AudioFormatInvalid) as exc:
        normalize_audio(bad, tmp_path)
    assert ".exe" in str(exc.value)


def test_normalize_rejects_magic_bytes_mismatch(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-4 — Given a file with a whitelisted extension (`.mp3`)
    but a magic-byte signature that doesn't match any audio format
    (here: a Windows PE header), When normalize_audio is invoked,
    Then AudioFormatInvalid is raised. Catches rename attacks.
    """
    from transcription_api.pipeline.normalize import (
        AudioFormatInvalid,
        normalize_audio,
    )

    masquerading = _write_with_ext(tmp_path, "evil.mp3", b"MZ\x90\x00" + b"\x00" * 64)
    with pytest.raises(AudioFormatInvalid):
        normalize_audio(masquerading, tmp_path)


def test_normalize_accepts_wav_magic(tmp_path: Path, monkeypatch):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-4 — Given a file with a valid `RIFF....WAVE` header,
    validation must pass even before ffmpeg is invoked. We stub the
    ffmpeg+ffprobe call so this test does not need the binaries.
    """
    from transcription_api.pipeline import normalize as norm_mod

    riff_header = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 32
    src = _write_with_ext(tmp_path, "ok.wav", riff_header)

    out = tmp_path / "out.wav"
    out.write_bytes(b"normalized-bytes")
    monkeypatch.setattr(
        norm_mod,
        "_run_ffmpeg_normalize",
        lambda src_path, dst_path: dst_path.write_bytes(b"normalized-bytes"),
    )
    monkeypatch.setattr(norm_mod, "_probe_duration_seconds", lambda _p: 1.5)

    out_path, audio_hash, duration = norm_mod.normalize_audio(src, tmp_path)
    assert out_path.suffix == ".wav"
    assert len(audio_hash) == 64
    assert duration == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# AC-1 — happy path (ffmpeg required)
# ---------------------------------------------------------------------------
@pytest.mark.requires_ffmpeg
def test_normalize_produces_wav_16khz_mono(tmp_path: Path, sine_mp3: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given a valid MP3 input, When normalize_audio runs,
    Then the output is a WAV at 16 kHz mono, the returned hash is a
    64-char SHA-256 hex of the normalized bytes, and the duration is > 0.
    """
    from transcription_api.pipeline.normalize import normalize_audio

    out_path, audio_hash, duration = normalize_audio(sine_mp3, tmp_path)

    assert out_path.exists(), "normalize_audio must emit the output file"
    assert out_path.suffix == ".wav"
    info = subprocess.check_output(  # noqa: S603,S607
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-select_streams",
            "a:0",
            str(out_path),
        ]
    )
    assert b"sample_rate=16000" in info, info
    assert b"channels=1" in info, info
    assert len(audio_hash) == 64
    assert all(c in "0123456789abcdef" for c in audio_hash)
    assert duration > 0


@pytest.mark.requires_ffmpeg
def test_normalize_hash_is_deterministic(tmp_path: Path, sine_mp3: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given the same input, two normalize_audio calls must
    yield the same audio_hash so the cache lookup (per-user, AC-12) is
    reproducible. Output paths differ (random per call) but the hashed
    content is identical.
    """
    from transcription_api.pipeline.normalize import normalize_audio

    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    out_a.mkdir()
    out_b.mkdir()
    _, hash_a, _ = normalize_audio(sine_mp3, out_a)
    _, hash_b, _ = normalize_audio(sine_mp3, out_b)
    assert hash_a == hash_b


@pytest.mark.requires_ffmpeg
def test_normalize_raises_pipeline_error_on_ffmpeg_failure(tmp_path: Path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: error catalog `PIPELINE_NORMALIZE_ERROR` — Given an input
    whose extension is whitelisted but whose contents ffmpeg can't decode,
    When normalize_audio is invoked, Then PipelineNormalizeError is raised
    so the orchestrator (Batch 5) can map it to HTTP 500
    PIPELINE_NORMALIZE_ERROR. We force the case by writing a wav header
    followed by garbage; ffmpeg will fail decoding the body.
    """
    from transcription_api.pipeline.normalize import (
        PipelineNormalizeError,
        normalize_audio,
    )

    # Valid RIFF/WAVE header so format validation passes, but body is junk
    # that ffmpeg can't render to PCM 16 kHz mono.
    bogus = _write_with_ext(
        tmp_path,
        "bogus.wav",
        b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\xff" * 1024,
    )
    with pytest.raises(PipelineNormalizeError):
        normalize_audio(bogus, tmp_path)


# ---------------------------------------------------------------------------
# SD-1 — MAX_AUDIO_DURATION_SECONDS hard cap (spec §7).
# ---------------------------------------------------------------------------
def test_normalize_raises_audio_too_long_when_duration_exceeds_cap(
    tmp_path: Path, monkeypatch
):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: SD-1 — Given a normalized audio whose ffprobe duration
    exceeds settings.max_audio_duration_seconds, When normalize_audio
    is invoked, Then AudioTooLong is raised with the actual duration
    and the configured cap. The API maps this to HTTP 413
    AUDIO_TOO_LARGE in api/transcriptions.py.
    """
    from transcription_api.config import settings
    from transcription_api.pipeline import normalize as norm_mod
    from transcription_api.pipeline.normalize import AudioTooLong, normalize_audio

    src = _write_with_ext(tmp_path, "long.mp3", b"ID3" + b"\xab" * 32)

    def fake_ffmpeg(_src, dst):
        Path(dst).write_bytes(b"RIFF\x24\x00\x00\x00WAVE" + b"\x00" * 32)

    monkeypatch.setattr(norm_mod, "_run_ffmpeg_normalize", fake_ffmpeg)
    # Force ffprobe to report a duration above the cap.
    monkeypatch.setattr(norm_mod, "_probe_duration_seconds", lambda _p: 9999.0)
    monkeypatch.setattr(settings, "max_audio_duration_seconds", 7200)

    with pytest.raises(AudioTooLong) as exc_info:
        normalize_audio(src, tmp_path)
    assert exc_info.value.duration_seconds == 9999.0
    assert exc_info.value.max_seconds == 7200


def test_normalize_accepts_duration_at_cap(tmp_path: Path, monkeypatch):
    """SD-1: a duration exactly at the cap (≤) is accepted, not rejected."""
    from transcription_api.config import settings
    from transcription_api.pipeline import normalize as norm_mod
    from transcription_api.pipeline.normalize import normalize_audio

    src = _write_with_ext(tmp_path, "edge.mp3", b"ID3" + b"\xab" * 32)

    def fake_ffmpeg(_src, dst):
        Path(dst).write_bytes(b"RIFF\x24\x00\x00\x00WAVE" + b"\x00" * 32)

    monkeypatch.setattr(norm_mod, "_run_ffmpeg_normalize", fake_ffmpeg)
    monkeypatch.setattr(norm_mod, "_probe_duration_seconds", lambda _p: 7200.0)
    monkeypatch.setattr(settings, "max_audio_duration_seconds", 7200)

    out_path, _hash, duration = normalize_audio(src, tmp_path)
    assert duration == pytest.approx(7200.0)
    assert out_path.exists()
