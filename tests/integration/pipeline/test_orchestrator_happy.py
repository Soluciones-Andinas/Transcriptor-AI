"""Orchestrator happy path: normalize → cache → STT → diarize → merge → persist.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-1 (orchestrator side) — Given a fresh audio file, When ``orchestrate``
  runs, Then it returns a dict with ``transcription_id``, ``audio_hash``,
  ``segments``, ``language``, ``duration_seconds``, ``num_speakers``,
  ``text_content`` AND inserts a row in ``transcriptions`` with
  ``user_id = current_user.id`` AND populates the per-user filesystem cache.
- ALT-1 — Given an audio whose ``(user_id, audio_hash)`` is already in the
  cache, When ``orchestrate`` runs, Then it skips STT+diarize+merge (mocks
  assert ``not called``), still INSERTs a fresh row (the user's history
  grows even when compute is reused), and the result is marked
  ``metadata.cache_hit: true``.
- ALT-2 — Given Whisper returns ``{"segments": []}`` (silent audio), When
  ``orchestrate`` runs, Then diarize+merge are skipped, the row is
  persisted with ``text_content=""``, ``num_speakers=0`` and
  ``metadata.silent_audio: true``.

These tests need a real Postgres (testcontainers); ``requires_docker``
auto-skips on machines without a daemon.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from tests.factories import make_user

pytestmark = pytest.mark.requires_docker


# ---------------------------------------------------------------------------
# Canonical pipeline outputs used across the file.
# ---------------------------------------------------------------------------
_AUDIO_HASH = "a" * 64  # 64 lowercase hex chars (CacheStore validates)
_DURATION = 12.34


def _whisper_result_two_words():
    return {
        "segments": [
            {
                "start": 0.0,
                "end": 1.0,
                "text": "Hola",
                "words": [{"word": "Hola", "start": 0.0, "end": 1.0}],
            },
            {
                "start": 1.0,
                "end": 2.0,
                "text": "mundo",
                "words": [{"word": "mundo", "start": 1.0, "end": 2.0}],
            },
        ],
        "language": "es",
    }


def _diarization_two_speakers():
    return [(0.0, 1.0, "SPEAKER_00"), (1.0, 2.0, "SPEAKER_01")]


def _build_kwargs(*, user_id, db, cache_store, upload_dir):
    return {
        "user_id": user_id,
        "db": db,
        "file_path": Path("/tmp/x.mp3"),
        "original_filename": "meeting.mp3",
        "original_size_bytes": 4096,
        "whisper_model": MagicMock(name="whisper_model"),
        "pyannote_pipeline": MagicMock(name="pyannote_pipeline"),
        "cache_store": cache_store,
        "upload_dir": upload_dir,
        "language": "es",
        "num_speakers": 2,
    }


# ---------------------------------------------------------------------------
# AC-1 — happy path
# ---------------------------------------------------------------------------
async def test_orchestrate_inserts_transcription_row_and_writes_cache(
    session, tmp_path
):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given a fresh audio, When orchestrate completes,
    Then a Transcription row exists in the DB with the expected
    user_id + audio_hash + language + segments AND the per-user cache
    has the result payload available for re-reads (ALT-1 tested below).
    """
    from transcription_api.pipeline.cache import CacheStore
    from transcription_api.pipeline.orchestrator import orchestrate

    user = await make_user(session)
    cache_store = CacheStore(base_dir=tmp_path / "cache")
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()

    wav_path = upload_dir / "x.normalized.wav"
    wav_path.write_bytes(b"fake-wav")

    with patch(
        "transcription_api.pipeline.normalize.normalize_audio",
        return_value=(wav_path, _AUDIO_HASH, _DURATION),
    ), patch(
        "transcription_api.pipeline.stt.transcribe",
        return_value=_whisper_result_two_words(),
    ), patch(
        "transcription_api.pipeline.diarize.diarize",
        return_value=_diarization_two_speakers(),
    ):
        result = await orchestrate(
            **_build_kwargs(
                user_id=user.id,
                db=session,
                cache_store=cache_store,
                upload_dir=upload_dir,
            )
        )

    # Result shape contract.
    assert "transcription_id" in result
    assert result["audio_hash"] == _AUDIO_HASH
    assert result["language"] == "es"
    assert result["num_speakers"] == 2
    assert len(result["segments"]) == 2
    # Word-level merge happened (each word has a speaker).
    assert result["segments"][0]["words"][0]["speaker"] == "SPEAKER_00"
    assert result["segments"][1]["words"][0]["speaker"] == "SPEAKER_01"

    # DB row.
    from transcription_api.db.models import Transcription

    row = await session.scalar(
        select(Transcription).where(Transcription.id == result["transcription_id"])
    )
    assert row is not None
    assert row.user_id == user.id
    assert row.audio_hash == _AUDIO_HASH
    assert row.language == "es"
    assert row.num_speakers == 2

    # Per-user cache populated.
    cached = cache_store.get(user.id, _AUDIO_HASH)
    assert cached is not None
    assert cached["audio_hash"] == _AUDIO_HASH


async def test_orchestrate_marks_cache_miss_in_metadata(session, tmp_path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — On a cache miss, ``metadata.cache_hit`` is False
    (default is False unless ALT-1 hit). Lets the API response and the
    Transcription row both reflect whether compute was reused.
    """
    from transcription_api.pipeline.cache import CacheStore
    from transcription_api.pipeline.orchestrator import orchestrate

    user = await make_user(session)
    cache_store = CacheStore(base_dir=tmp_path / "cache")
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    wav_path = upload_dir / "x.normalized.wav"
    wav_path.write_bytes(b"fake")

    with patch(
        "transcription_api.pipeline.normalize.normalize_audio",
        return_value=(wav_path, _AUDIO_HASH, _DURATION),
    ), patch(
        "transcription_api.pipeline.stt.transcribe",
        return_value=_whisper_result_two_words(),
    ), patch(
        "transcription_api.pipeline.diarize.diarize",
        return_value=_diarization_two_speakers(),
    ):
        result = await orchestrate(
            **_build_kwargs(
                user_id=user.id,
                db=session,
                cache_store=cache_store,
                upload_dir=upload_dir,
            )
        )

    assert result["metadata"]["cache_hit"] is False


# ---------------------------------------------------------------------------
# ALT-1 — cache hit path
# ---------------------------------------------------------------------------
async def test_orchestrate_cache_hit_skips_stt_and_diarize(session, tmp_path):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: ALT-1 — Given a cache pre-populated with the same
    (user_id, audio_hash), When orchestrate runs, Then STT and diarize
    mocks are NOT called (the compute is reused), AND a fresh row is
    still INSERTed (the user's history grows on every upload), AND
    ``metadata.cache_hit: true`` is set on both the response and the row.
    """
    from transcription_api.db.models import Transcription
    from transcription_api.pipeline.cache import CacheStore
    from transcription_api.pipeline.orchestrator import orchestrate

    user = await make_user(session)
    cache_store = CacheStore(base_dir=tmp_path / "cache")
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    wav_path = upload_dir / "x.normalized.wav"
    wav_path.write_bytes(b"fake")

    # Pre-populate the cache with a payload as if a prior run produced it.
    prior_payload = {
        "audio_hash": _AUDIO_HASH,
        "language": "es",
        "duration_seconds": _DURATION,
        "num_speakers": 2,
        "text_content": "Hola SPEAKER_00\nmundo SPEAKER_01",
        "segments": _whisper_result_two_words()["segments"],
        "metadata": {
            "model": "whisper-large-v3",
            "diarizer": "pyannote-3.1",
        },
    }
    cache_store.put(user.id, _AUDIO_HASH, prior_payload)

    with patch(
        "transcription_api.pipeline.normalize.normalize_audio",
        return_value=(wav_path, _AUDIO_HASH, _DURATION),
    ), patch(
        "transcription_api.pipeline.stt.transcribe"
    ) as stt_mock, patch(
        "transcription_api.pipeline.diarize.diarize"
    ) as diarize_mock:
        result = await orchestrate(
            **_build_kwargs(
                user_id=user.id,
                db=session,
                cache_store=cache_store,
                upload_dir=upload_dir,
            )
        )

    stt_mock.assert_not_called()
    diarize_mock.assert_not_called()
    assert result["metadata"]["cache_hit"] is True
    # Fresh row still inserted — history grows.
    row = await session.scalar(
        select(Transcription).where(Transcription.id == result["transcription_id"])
    )
    assert row is not None
    assert row.user_id == user.id
    assert row.audio_hash == _AUDIO_HASH


# ---------------------------------------------------------------------------
# ALT-2 — silent audio
# ---------------------------------------------------------------------------
async def test_orchestrate_silent_audio_persists_with_empty_text(
    session, tmp_path
):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: ALT-2 — Given Whisper returns segments=[] (silent audio),
    When orchestrate runs, Then diarize is NOT called, the row is
    persisted with text_content="", num_speakers=0, segments=[], and
    metadata.silent_audio=True. The transcription is real (not an
    error), just empty.
    """
    from transcription_api.db.models import Transcription
    from transcription_api.pipeline.cache import CacheStore
    from transcription_api.pipeline.orchestrator import orchestrate

    user = await make_user(session)
    cache_store = CacheStore(base_dir=tmp_path / "cache")
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    wav_path = upload_dir / "x.normalized.wav"
    wav_path.write_bytes(b"fake")

    with patch(
        "transcription_api.pipeline.normalize.normalize_audio",
        return_value=(wav_path, _AUDIO_HASH, _DURATION),
    ), patch(
        "transcription_api.pipeline.stt.transcribe",
        return_value={"segments": [], "language": "es"},
    ), patch(
        "transcription_api.pipeline.diarize.diarize"
    ) as diarize_mock:
        result = await orchestrate(
            **_build_kwargs(
                user_id=user.id,
                db=session,
                cache_store=cache_store,
                upload_dir=upload_dir,
            )
        )

    diarize_mock.assert_not_called()
    assert result["text_content"] == ""
    assert result["num_speakers"] == 0
    assert result["segments"] == []
    assert result["metadata"].get("silent_audio") is True

    row = await session.scalar(
        select(Transcription).where(Transcription.id == result["transcription_id"])
    )
    assert row is not None
    assert row.text_content == ""
    assert row.num_speakers == 0
    assert row.extra_metadata.get("silent_audio") is True


# ---------------------------------------------------------------------------
# Cleanup invariant
# ---------------------------------------------------------------------------
async def test_orchestrate_cleans_up_normalized_wav_after_success(
    session, tmp_path
):
    """
    Spec: SPEC-capa3-pipeline-v1 §2.1
    Criterion: cleanup invariant — After a successful orchestrate, the
    normalized WAV file in upload_dir is unlinked. The orchestrator
    owns the lifecycle of the temp file (Privacy > Cost — don't keep
    intermediate audio around).
    """
    from transcription_api.pipeline.cache import CacheStore
    from transcription_api.pipeline.orchestrator import orchestrate

    user = await make_user(session)
    cache_store = CacheStore(base_dir=tmp_path / "cache")
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    wav_path = upload_dir / "x.normalized.wav"
    wav_path.write_bytes(b"fake")

    with patch(
        "transcription_api.pipeline.normalize.normalize_audio",
        return_value=(wav_path, _AUDIO_HASH, _DURATION),
    ), patch(
        "transcription_api.pipeline.stt.transcribe",
        return_value=_whisper_result_two_words(),
    ), patch(
        "transcription_api.pipeline.diarize.diarize",
        return_value=_diarization_two_speakers(),
    ):
        await orchestrate(
            **_build_kwargs(
                user_id=user.id,
                db=session,
                cache_store=cache_store,
                upload_dir=upload_dir,
            )
        )

    assert not wav_path.exists(), (
        "normalized wav must be cleaned up after success (cache lives "
        "in cache_store, not in upload_dir)"
    )


# ---------------------------------------------------------------------------
# Defense: bogus audio_hash from normalize is rejected (unreachable in prod
# but matters for defense-in-depth: the orchestrator does not bypass the
# CacheStore validation contract).
# ---------------------------------------------------------------------------
async def test_orchestrate_rejects_invalid_audio_hash_from_normalize(
    session, tmp_path
):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: defense-in-depth — Given a buggy normalize_audio that
    returns an invalid audio_hash (e.g. with path-traversal segments),
    When orchestrate runs, Then ValueError propagates up from the
    CacheStore validation. The orchestrator does NOT silently rewrite
    the hash to keep the cache layer honest.
    """
    from transcription_api.pipeline.cache import CacheStore
    from transcription_api.pipeline.orchestrator import orchestrate

    user = await make_user(session)
    cache_store = CacheStore(base_dir=tmp_path / "cache")
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    wav_path = upload_dir / "x.normalized.wav"
    wav_path.write_bytes(b"fake")

    bad_hash = "../../etc/passwd"
    with patch(
        "transcription_api.pipeline.normalize.normalize_audio",
        return_value=(wav_path, bad_hash, _DURATION),
    ):
        with pytest.raises(ValueError):
            await orchestrate(
                **_build_kwargs(
                    user_id=user.id,
                    db=session,
                    cache_store=cache_store,
                    upload_dir=upload_dir,
                )
            )

    # Lock released on the propagated error.
    from transcription_api.pipeline.orchestrator import _orchestrator_lock

    assert not _orchestrator_lock.locked()
