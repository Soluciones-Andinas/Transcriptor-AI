"""Audio normalization to 16 kHz mono WAV with content-derived hash.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-1 — `normalize_audio` returns (output_wav_path, sha256_hex, duration_seconds).
  The hash is computed over the normalized WAV bytes so two inputs with
  identical PCM but different containers (mp3 vs m4a) collide on the cache
  key. The cache key in Batch 5 is (user_id, audio_hash) — see D-027.
- AC-4 — Format validation rejects inputs whose extension or magic bytes
  fall outside the whitelist (mp4, mp3, m4a, wav, flac). Validation runs
  in pure Python BEFORE shelling out to ffmpeg, so a 400 response can be
  produced without paying the subprocess cost or leaking ffmpeg errors.

Error taxonomy:
- ``AudioFormatInvalid`` — maps to HTTP 400 ``AUDIO_FORMAT_INVALID`` at the
  API boundary (Batch 6).
- ``PipelineNormalizeError`` — maps to HTTP 500 ``PIPELINE_NORMALIZE_ERROR``
  at the API boundary; carries the ffmpeg/ffprobe failure detail for logs.

The two helpers ``_run_ffmpeg_normalize`` and ``_probe_duration_seconds``
are intentionally module-level so unit tests can patch them out and exercise
the validation + hashing logic without ffmpeg installed.

Capa 3 review CR-3:
- Magic-byte check uses bounded ``open() + read(64)`` instead of
  ``read_bytes()[:64]`` (which loads the entire file into RAM just to
  slice 64 bytes — a 500 MB upload triggered a 500 MB allocation per
  request).
- SHA-256 is computed via a chunked stream (64 KB blocks) instead of
  loading the full WAV into RAM. A 2 h normalized WAV is ~230 MB; the
  chunked path keeps the working set bounded regardless of duration.
"""
from __future__ import annotations

import hashlib
import secrets
import subprocess
from pathlib import Path

# Whitelist (extension lowercase without leading dot).
_ALLOWED_EXTENSIONS = frozenset({"mp3", "mp4", "m4a", "wav", "flac"})

# CR-3: bounded read for magic-byte check + chunk size for streaming SHA-256.
_MAGIC_HEAD_BYTES = 64
_SHA256_CHUNK_BYTES = 64 * 1024  # 64 KB — tuned for filesystem block alignment


def _read_magic_head(path: Path) -> bytes:
    """Read at most ``_MAGIC_HEAD_BYTES`` from the start of ``path``.

    CR-3: previously ``path.read_bytes()[:64]`` materialized the WHOLE file
    in memory (a 500 MB mp4 upload triggered a 500 MB allocation per
    validation request). The bounded ``read(N)`` keeps RAM usage at ~64
    bytes regardless of file size.
    """
    with path.open("rb") as fp:
        return fp.read(_MAGIC_HEAD_BYTES)


def _sha256_file(path: Path) -> str:
    """Stream-hash ``path`` in 64 KB chunks; return the hex digest.

    CR-3: previously ``hashlib.sha256(path.read_bytes()).hexdigest()`` held
    the entire file in RAM. For a 2 h WAV (~230 MB) that's an avoidable
    spike; the chunked variant keeps working set bounded.
    """
    h = hashlib.sha256()
    with path.open("rb") as fp:
        while chunk := fp.read(_SHA256_CHUNK_BYTES):
            h.update(chunk)
    return h.hexdigest()


class AudioFormatInvalid(Exception):
    """Raised when the input is not a recognized audio container.

    Carries a human-readable Spanish reason for the API surface (matches
    the spec error_catalog body shape: ``{ error_code, reason }``).
    """


class PipelineNormalizeError(Exception):
    """Raised when ffmpeg or ffprobe cannot process a format-validated input.

    Means: extension+magic bytes were OK but actual decoding failed (corrupt
    file, unsupported codec inside container, ffmpeg internal error). The
    API maps this to HTTP 500 ``PIPELINE_NORMALIZE_ERROR``.
    """


def _magic_matches(blob: bytes, ext: str) -> bool:
    """Return True if the first bytes of ``blob`` look like ``ext``.

    Defensive: only the unambiguous, well-documented signatures are checked.
    Subtle variants (mp3 frame at non-zero offset, mp4 with leading free box)
    are out of scope; the file would still go through ffmpeg's own decode
    path for the actual normalization, so we only need a coarse gate.
    """
    if ext == "wav":
        return blob[:4] == b"RIFF" and blob[8:12] == b"WAVE"
    if ext == "flac":
        return blob[:4] == b"fLaC"
    if ext == "mp3":
        if blob[:3] == b"ID3":
            return True
        # MPEG audio frame sync: 11 bits set high (0xFFE).
        if len(blob) >= 2 and blob[0] == 0xFF and (blob[1] & 0xE0) == 0xE0:
            return True
        return False
    if ext in {"mp4", "m4a"}:
        # ISO Base Media File Format: 'ftyp' box at offset 4..8.
        return blob[4:8] == b"ftyp"
    return False


def _run_ffmpeg_normalize(src: Path, dst: Path) -> None:
    """Re-encode ``src`` to PCM 16-bit / 16 kHz / mono WAV at ``dst``.

    Indirection level so unit tests can patch this out and exercise the
    surrounding validation + hashing without ffmpeg installed.
    """
    subprocess.run(  # noqa: S603,S607
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(dst),
        ],
        check=True,
        capture_output=True,
    )


def _probe_duration_seconds(path: Path) -> float:
    """Return the duration of ``path`` in seconds via ffprobe.

    Module-level for the same patchability reason as ``_run_ffmpeg_normalize``.
    """
    raw = subprocess.check_output(  # noqa: S603,S607
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
    )
    return float(raw.decode().strip())


def normalize_audio(src: Path, output_dir: Path) -> tuple[Path, str, float]:
    """Validate, normalize, hash, and probe the duration of an input file.

    Steps:
    1. Reject extensions outside the whitelist (AC-4).
    2. Reject content whose magic bytes don't match the extension (AC-4).
    3. Run ffmpeg to produce a 16 kHz mono PCM WAV in ``output_dir``.
    4. Compute SHA-256 of the WAV bytes — this is the ``audio_hash``.
    5. Probe duration via ffprobe.

    Returns ``(output_wav_path, audio_hash_hex, duration_seconds)``. Raises
    ``AudioFormatInvalid`` on validation failure, ``PipelineNormalizeError``
    on ffmpeg/ffprobe failure.
    """
    ext = src.suffix.lower().lstrip(".")
    if ext not in _ALLOWED_EXTENSIONS:
        raise AudioFormatInvalid(
            f"extensión .{ext} no soportada; soportadas: mp4, mp3, m4a, wav, flac"
        )

    # CR-3: bounded read instead of read_bytes()[:64] which loaded whole file.
    head = _read_magic_head(src) if src.is_file() else b""
    if not _magic_matches(head, ext):
        raise AudioFormatInvalid(
            f"el archivo declara extensión .{ext} pero su contenido no lo es"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    # Random suffix avoids collisions when multiple uploads share a stem;
    # the orchestrator is responsible for cleanup of the path in `finally`.
    out_path = output_dir / f"{secrets.token_hex(8)}.normalized.wav"

    try:
        _run_ffmpeg_normalize(src, out_path)
    except subprocess.CalledProcessError as exc:
        # Best-effort cleanup: ffmpeg may have written a partial file.
        if out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass
        stderr = (exc.stderr or b"").decode(errors="replace")
        raise PipelineNormalizeError(
            f"ffmpeg failed (rc={exc.returncode}): {stderr.strip() or 'no stderr'}"
        ) from exc

    # CR-3: streaming SHA-256 instead of read_bytes() (avoids loading full WAV).
    audio_hash = _sha256_file(out_path)

    try:
        duration = _probe_duration_seconds(out_path)
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise PipelineNormalizeError(f"ffprobe failed: {exc}") from exc

    return out_path, audio_hash, duration
