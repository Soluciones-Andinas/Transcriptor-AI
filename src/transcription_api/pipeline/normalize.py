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

    Note: this hashes the FULL file including any RIFF metadata chunks
    written by ffmpeg. Use ``_sha256_wav_pcm`` for normalize's audio_hash
    so the cache key stays stable across ffmpeg versions (SD-3).
    """
    h = hashlib.sha256()
    with path.open("rb") as fp:
        while chunk := fp.read(_SHA256_CHUNK_BYTES):
            h.update(chunk)
    return h.hexdigest()


def _sha256_wav_pcm(path: Path) -> str:
    """Stream-hash ONLY the PCM 'data' chunk of a WAV file.

    SD-3: ffmpeg may write LIST INFO, bext, JUNK, or other RIFF
    sub-chunks alongside the audio data. Hashing the whole file makes
    ``audio_hash`` ffmpeg-version-dependent — a Docker rebuild that
    bumps ffmpeg invalidates every existing cache entry without the
    PCM bytes having changed. By isolating the ``data`` chunk we get
    a content-only hash that is stable across ffmpeg versions.

    Algorithm:
    1. Read the 12-byte RIFF header. If not a canonical RIFF/WAVE,
       fall back to ``_sha256_file`` (defensive — covers tests that
       pass non-WAV files and unexpected ffmpeg outputs).
    2. Walk the RIFF sub-chunks. Each starts with 8 bytes:
       ``<chunk_id (4)><chunk_size_le (4)>``.
    3. When ``chunk_id == b"data"``, hash exactly ``chunk_size`` bytes
       (in 64 KB blocks) and return.
    4. Otherwise skip the chunk (pad to even byte boundary per RIFF
       spec) and continue. Tolerate truncation gracefully.
    """
    h = hashlib.sha256()
    with path.open("rb") as fp:
        riff = fp.read(12)
        if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            # Not a WAV (or truncated) — fall back to full-file hash.
            fp.seek(0)
            while chunk := fp.read(_SHA256_CHUNK_BYTES):
                h.update(chunk)
            return h.hexdigest()

        # Walk sub-chunks until we find "data" (or run out of file).
        while True:
            header = fp.read(8)
            if len(header) < 8:
                break
            chunk_id = header[:4]
            chunk_size = int.from_bytes(header[4:8], "little")
            if chunk_id == b"data":
                remaining = chunk_size
                while remaining > 0:
                    block = fp.read(min(_SHA256_CHUNK_BYTES, remaining))
                    if not block:
                        break
                    h.update(block)
                    remaining -= len(block)
                # Canonical WAV has one data chunk; stop here.
                break
            # Non-data chunks: skip the body (RIFF spec pads to even).
            skip = chunk_size + (chunk_size & 1)
            fp.seek(skip, 1)
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


class AudioTooLong(Exception):
    """Raised when the input duration exceeds ``MAX_AUDIO_DURATION_SECONDS``.

    SD-1 (spec §7): bound on the pipeline cost. Enforced AFTER ffprobe
    measures the duration (file size doesn't determine duration — bitrate
    varies by codec). The API maps this to HTTP 413 ``AUDIO_TOO_LARGE``
    with ``duration_seconds`` and ``max_seconds`` in the response body.
    """

    def __init__(self, duration_seconds: float, max_seconds: int) -> None:
        self.duration_seconds = duration_seconds
        self.max_seconds = max_seconds
        super().__init__(
            f"audio duration {duration_seconds:.1f}s exceeds {max_seconds}s cap"
        )


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


def _detect_ext_by_magic(blob: bytes) -> str | None:
    """Infer the canonical extension from magic bytes; ``None`` if unknown.

    Mirror of ``_magic_matches`` but answering "what is this?" instead of
    "is this an X?". Lets ``normalize_audio`` recover when the on-disk
    suffix is wrong or missing — D-048 documents that the upload endpoint
    persists every file as ``original.bin`` because ``upload_sessions``
    has no ``original_filename`` column yet, so the extension-only guard
    rejects every legitimate audio upload coming through the chunked path.

    ISOBMFF containers (mp4/m4a/mov) collapse to ``"mp4"`` — ffmpeg's
    audio extraction is identical regardless of brand.
    """
    if blob[:4] == b"RIFF" and blob[8:12] == b"WAVE":
        return "wav"
    if blob[:4] == b"fLaC":
        return "flac"
    if blob[:3] == b"ID3":
        return "mp3"
    if len(blob) >= 2 and blob[0] == 0xFF and (blob[1] & 0xE0) == 0xE0:
        return "mp3"
    if blob[4:8] == b"ftyp":
        return "mp4"
    return None


# H-1: hard timeouts so a hung ffmpeg/ffprobe never blocks the worker
# thread indefinitely. asyncio.to_thread cannot kill a sync call
# already in progress; the subprocess timeout is the only mechanism
# that bounds the work. Numbers chosen well above expected runtimes:
# - 600s (10min) for normalize: realtime decode of a 1h audio is <2min
#   on modern CPU, 4060 Ti can do 2h in <5min. 10min is generous tail.
# - 30s for ffprobe: header read should be sub-second; 30s catches
#   pathological filesystem stalls without false positives.
_FFMPEG_TIMEOUT_SECONDS = 600
_FFPROBE_TIMEOUT_SECONDS = 30


def _run_ffmpeg_normalize(src: Path, dst: Path) -> None:
    """Re-encode ``src`` to PCM 16-bit / 16 kHz / mono WAV at ``dst``.

    Indirection level so unit tests can patch this out and exercise the
    surrounding validation + hashing without ffmpeg installed. H-1: hard
    timeout via subprocess.run keyword bounds runaway invocations.
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
        timeout=_FFMPEG_TIMEOUT_SECONDS,
    )


def _probe_duration_seconds(path: Path) -> float:
    """Return the duration of ``path`` in seconds via ffprobe.

    Module-level for the same patchability reason as ``_run_ffmpeg_normalize``.
    H-1: hard timeout — a stalled ffprobe is a filesystem / driver issue
    and should surface fast as PipelineNormalizeError, not block the loop.
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
        timeout=_FFPROBE_TIMEOUT_SECONDS,
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
    # CR-3: bounded read once; reused for fallback inference + magic match.
    head = _read_magic_head(src) if src.is_file() else b""
    ext = src.suffix.lower().lstrip(".")

    if ext not in _ALLOWED_EXTENSIONS:
        # Magic-byte fallback (D-048 + Capa 4 chunked upload): the upload
        # endpoint persists every file as ``original.bin`` because
        # ``upload_sessions`` has no original_filename column yet. The
        # extension-only guard would reject every legitimate audio upload.
        # Infer from magic bytes; raise only if both suffix and content
        # are unrecognized.
        inferred = _detect_ext_by_magic(head)
        if inferred is None:
            raise AudioFormatInvalid(
                f"extensión .{ext} no soportada y magic-bytes no reconocidos; "
                "soportadas: mp4, mp3, m4a, wav, flac"
            )
        ext = inferred
    elif not _magic_matches(head, ext):
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
    except subprocess.TimeoutExpired as exc:
        # H-1: ffmpeg exceeded the hard subprocess timeout. Best-effort
        # cleanup of any partial output, then surface as a typed error so
        # the orchestrator releases the lock + the API maps to 500.
        if out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass
        raise PipelineNormalizeError(
            f"ffmpeg exceeded {_FFMPEG_TIMEOUT_SECONDS}s timeout"
        ) from exc

    # SD-3: hash ONLY the PCM data chunk so audio_hash is stable across
    # ffmpeg versions. Fallback to full-file hash for non-WAV inputs is
    # built into ``_sha256_wav_pcm`` (defense for tests / future changes).
    audio_hash = _sha256_wav_pcm(out_path)

    try:
        duration = _probe_duration_seconds(out_path)
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise PipelineNormalizeError(f"ffprobe failed: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PipelineNormalizeError(
            f"ffprobe exceeded {_FFPROBE_TIMEOUT_SECONDS}s timeout"
        ) from exc

    # SD-1: enforce duration cap. We do this AFTER probe (not before
    # ffmpeg) because file size doesn't reliably bound duration —
    # bitrate varies wildly by codec. The cost of normalizing first
    # then rejecting is bounded by _FFMPEG_TIMEOUT_SECONDS.
    from ..config import settings as _settings  # avoid circular import at module load

    max_secs = _settings.max_audio_duration_seconds
    if duration > max_secs:
        # Cleanup the normalized WAV: we won't be using it.
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise AudioTooLong(duration_seconds=duration, max_seconds=max_secs)

    return out_path, audio_hash, duration
