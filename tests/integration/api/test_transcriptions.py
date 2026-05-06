"""POST + GET /api/transcriptions integration tests.

Spec: SPEC-capa3-pipeline-v1
Covers (Batch 6):
- AC-1 — POST returns 200 + JSON with transcription_id, audio_hash, language,
  segments, duration_seconds, num_speakers, text_content (orchestrator's dict).
- AC-3 — Missing or malformed bearer -> 401 AUTH_NOT_AUTHENTICATED.
- AC-4 — File whose extension/content fails AudioFormatInvalid in normalize
  bubbles to 400 AUDIO_FORMAT_INVALID.
- AC-5 — Content-Length > MAX_UPLOAD_MB*1024*1024 -> 413 AUDIO_TOO_LARGE
  BEFORE reading the body (cheap pre-check).
- AC-8 — User B with B's bearer GETs user A's transcription_id -> 404
  TRANSCRIPTION_NOT_FOUND (no existence leak; ADR-014/015 listener filters).
- AC-13 — GET own transcription -> 200 + same shape as POST.
- AC-15 — When app.state.{whisper,pyannote}_status != 'ready', POST returns
  503 MODELS_NOT_LOADED with the failed model and pyannote_detail (when
  applicable) propagated.

Plus error mapping for orchestrator-typed exceptions: GPUBusy -> 503,
PipelineTimeout -> 504, GPUError -> 500, PipelineDiarizeError -> 500,
PipelineNormalizeError -> 500, generic Exception -> 500 INTERNAL_ERROR.

These tests need a real Postgres (testcontainers); ``requires_docker``
auto-skips on machines without a daemon. The orchestrator itself is
patched at ``transcription_api.api.transcriptions.orchestrate`` so neither
ffmpeg nor torch nor pyannote is exercised.
"""
from __future__ import annotations

import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.factories import make_bearer, make_transcription, make_user
from transcription_api.auth.mcp_bearer import generate_bearer

pytestmark = pytest.mark.requires_docker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def app_with_models_ready():
    """Boot the FastAPI app with models patched to 'ready'.

    The lifespan loaders are mocked so the lifespan path sets
    `app.state.{whisper,pyannote}_status = "ready"` without touching torch.
    """
    from transcription_api.main import app

    with patch(
        "transcription_api.pipeline.stt.load_whisper_model",
        return_value=MagicMock(name="whisper_model"),
    ), patch(
        "transcription_api.pipeline.diarize.load_pyannote_pipeline",
        return_value=MagicMock(name="pyannote_pipeline"),
    ):
        async with LifespanManager(app):
            yield app


@pytest.fixture
async def client(app_with_models_ready):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_models_ready),
        base_url="http://testserver",
        follow_redirects=False,
    ) as c:
        yield c


async def _seed_user_with_bearer(session, *, email_suffix: str):
    """Create a User + active McpBearer; return (user, plaintext)."""
    user = await make_user(session, email=f"user-{email_suffix}@x")
    plaintext, token_hash = generate_bearer()
    await make_bearer(session, user_id=user.id, token_hash=token_hash)
    await session.commit()
    return user, plaintext


def _orchestrator_result(*, transcription_id: uuid.UUID, audio_hash: str):
    """Canonical result dict shape returned by orchestrate() — matches
    the spec response."""
    return {
        "transcription_id": transcription_id,
        "audio_hash": audio_hash,
        "language": "es",
        "duration_seconds": 12.34,
        "num_speakers": 2,
        "text_content": "SPEAKER_00: Hola\nSPEAKER_01: mundo",
        "segments": [
            {
                "start": 0.0,
                "end": 1.0,
                "text": "Hola",
                "speaker": "SPEAKER_00",
                "words": [
                    {"word": "Hola", "start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"},
                ],
            },
        ],
        "metadata": {
            "model": "large-v3",
            "diarizer": "pyannote/speaker-diarization-3.1",
            "compute_type": "int8_float16",
            "cache_hit": False,
        },
    }


# ---------------------------------------------------------------------------
# AC-1 + AC-3 — happy path + 401
# ---------------------------------------------------------------------------
async def test_post_transcription_with_valid_bearer_returns_200(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-1 — Given a valid bearer + an audio file + the
    orchestrator returning the canonical result, When POST runs, Then
    the response is 200 + the orchestrator's dict (with transcription_id
    serialized as string for JSON).
    """
    user, plaintext = await _seed_user_with_bearer(session, email_suffix="ac1")
    transcription_id = uuid.uuid4()
    expected = _orchestrator_result(
        transcription_id=transcription_id, audio_hash="b" * 64
    )

    with patch(
        "transcription_api.api.transcriptions.orchestrate",
        new=AsyncMock(return_value=expected),
    ):
        r = await client.post(
            "/api/transcriptions",
            files={"file": ("meeting.mp3", io.BytesIO(b"ID3" + b"\x00" * 32), "audio/mpeg")},
            data={"language": "es"},
            headers={"authorization": f"Bearer {plaintext}"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcription_id"] == str(transcription_id)
    assert body["audio_hash"] == "b" * 64
    assert body["language"] == "es"
    assert body["num_speakers"] == 2
    assert body["segments"][0]["words"][0]["speaker"] == "SPEAKER_00"


async def test_post_transcription_without_bearer_returns_401(client):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-3 — Given no Authorization header, When POST runs,
    Then 401 AUTH_NOT_AUTHENTICATED is returned (no body content
    leaks; the dependency short-circuits before the route body runs).
    """
    r = await client.post(
        "/api/transcriptions",
        files={"file": ("x.mp3", io.BytesIO(b"ID3"), "audio/mpeg")},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["error_code"] == "AUTH_NOT_AUTHENTICATED"


async def test_post_transcription_with_malformed_bearer_returns_401(client):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-3 — Authorization header that is not 'Bearer <token>'
    -> 401 AUTH_NOT_AUTHENTICATED. Capa 2's get_current_user_mcp
    handles the parsing; Batch 6 just relies on the dependency.
    """
    r = await client.post(
        "/api/transcriptions",
        files={"file": ("x.mp3", io.BytesIO(b"ID3"), "audio/mpeg")},
        headers={"authorization": "Basic dXNlcjpwYXNz"},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["error_code"] == "AUTH_NOT_AUTHENTICATED"


# ---------------------------------------------------------------------------
# Error mapping — orchestrator typed errors -> HTTP per spec §4
# ---------------------------------------------------------------------------
async def test_post_maps_gpu_busy_to_503_with_retry_after(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1 §4
    Criterion: GPU_BUSY -> 503 + Retry-After: <retry_after> header.
    Distinct from PIPELINE_TIMEOUT (504) — semantic split per AC-6 vs AC-11.
    """
    from transcription_api.pipeline.orchestrator import GPUBusy

    user, plaintext = await _seed_user_with_bearer(session, email_suffix="busy")

    with patch(
        "transcription_api.api.transcriptions.orchestrate",
        new=AsyncMock(side_effect=GPUBusy(retry_after=600)),
    ):
        r = await client.post(
            "/api/transcriptions",
            files={"file": ("x.mp3", io.BytesIO(b"ID3"), "audio/mpeg")},
            headers={"authorization": f"Bearer {plaintext}"},
        )

    assert r.status_code == 503
    assert r.json()["detail"]["error_code"] == "GPU_BUSY"
    assert r.json()["detail"]["retry_after"] == 600
    assert r.headers.get("retry-after") == "600"


async def test_post_maps_pipeline_timeout_to_504(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1 §4
    Criterion: PIPELINE_TIMEOUT -> 504 with timeout_seconds in body.
    """
    from transcription_api.pipeline.orchestrator import PipelineTimeout

    user, plaintext = await _seed_user_with_bearer(session, email_suffix="timeout")

    with patch(
        "transcription_api.api.transcriptions.orchestrate",
        new=AsyncMock(side_effect=PipelineTimeout(timeout_seconds=1800)),
    ):
        r = await client.post(
            "/api/transcriptions",
            files={"file": ("x.mp3", io.BytesIO(b"ID3"), "audio/mpeg")},
            headers={"authorization": f"Bearer {plaintext}"},
        )

    assert r.status_code == 504
    assert r.json()["detail"]["error_code"] == "PIPELINE_TIMEOUT"
    assert r.json()["detail"]["timeout_seconds"] == 1800


async def test_post_maps_gpu_error_to_500(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1 §4
    Criterion: GPU_ERROR (CUDA OOM, etc.) -> 500.
    """
    from transcription_api.pipeline.stt import GPUError

    user, plaintext = await _seed_user_with_bearer(session, email_suffix="gpuerr")

    with patch(
        "transcription_api.api.transcriptions.orchestrate",
        new=AsyncMock(side_effect=GPUError("oom", "CUDA out of memory")),
    ):
        r = await client.post(
            "/api/transcriptions",
            files={"file": ("x.mp3", io.BytesIO(b"ID3"), "audio/mpeg")},
            headers={"authorization": f"Bearer {plaintext}"},
        )

    assert r.status_code == 500
    assert r.json()["detail"]["error_code"] == "GPU_ERROR"


async def test_post_maps_pipeline_normalize_error_to_500(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1 §4
    Criterion: PIPELINE_NORMALIZE_ERROR -> 500. Distinct from
    AUDIO_FORMAT_INVALID (400) — normalize errors are runtime
    failures of ffmpeg, format invalid is the file declaring the
    wrong shape upfront.
    """
    from transcription_api.pipeline.normalize import PipelineNormalizeError

    user, plaintext = await _seed_user_with_bearer(session, email_suffix="norm")

    with patch(
        "transcription_api.api.transcriptions.orchestrate",
        new=AsyncMock(side_effect=PipelineNormalizeError("ffmpeg returned -1")),
    ):
        r = await client.post(
            "/api/transcriptions",
            files={"file": ("x.mp3", io.BytesIO(b"ID3"), "audio/mpeg")},
            headers={"authorization": f"Bearer {plaintext}"},
        )

    assert r.status_code == 500
    assert r.json()["detail"]["error_code"] == "PIPELINE_NORMALIZE_ERROR"


async def test_post_maps_pipeline_diarize_error_to_500(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1 §4
    Criterion: PIPELINE_DIARIZE_ERROR -> 500.
    """
    from transcription_api.pipeline.diarize import PipelineDiarizeError

    user, plaintext = await _seed_user_with_bearer(session, email_suffix="diar")

    with patch(
        "transcription_api.api.transcriptions.orchestrate",
        new=AsyncMock(side_effect=PipelineDiarizeError("pyannote internal")),
    ):
        r = await client.post(
            "/api/transcriptions",
            files={"file": ("x.mp3", io.BytesIO(b"ID3"), "audio/mpeg")},
            headers={"authorization": f"Bearer {plaintext}"},
        )

    assert r.status_code == 500
    assert r.json()["detail"]["error_code"] == "PIPELINE_DIARIZE_ERROR"


async def test_post_maps_unexpected_exception_to_500_with_error_id(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1 §4
    Criterion: INTERNAL_ERROR (catch-all) -> 500 + body with
    ``error_id`` UUID so the operator can correlate the response with
    the traceback in the logs (the traceback itself is NOT echoed —
    that would be a leak).
    """
    user, plaintext = await _seed_user_with_bearer(session, email_suffix="unexp")

    with patch(
        "transcription_api.api.transcriptions.orchestrate",
        new=AsyncMock(side_effect=RuntimeError("totally unexpected")),
    ):
        r = await client.post(
            "/api/transcriptions",
            files={"file": ("x.mp3", io.BytesIO(b"ID3"), "audio/mpeg")},
            headers={"authorization": f"Bearer {plaintext}"},
        )

    assert r.status_code == 500
    body = r.json()
    assert body["detail"]["error_code"] == "INTERNAL_ERROR"
    assert "error_id" in body["detail"]
    # Verify it's a parseable UUID (correlation key for log search).
    uuid.UUID(body["detail"]["error_id"])


# ---------------------------------------------------------------------------
# AC-4 — extension / format validation
# ---------------------------------------------------------------------------
async def test_post_rejects_audio_format_invalid_with_400(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-4 — When orchestrator (via normalize) raises
    AudioFormatInvalid, the API maps it to 400 AUDIO_FORMAT_INVALID.
    """
    from transcription_api.pipeline.normalize import AudioFormatInvalid

    user, plaintext = await _seed_user_with_bearer(session, email_suffix="fmt")

    with patch(
        "transcription_api.api.transcriptions.orchestrate",
        new=AsyncMock(
            side_effect=AudioFormatInvalid(
                "extensión .exe no soportada; soportadas: mp4, mp3, m4a, wav, flac"
            )
        ),
    ):
        r = await client.post(
            "/api/transcriptions",
            files={"file": ("evil.exe", io.BytesIO(b"MZ\x90\x00"), "application/octet-stream")},
            headers={"authorization": f"Bearer {plaintext}"},
        )

    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "AUDIO_FORMAT_INVALID"


# ---------------------------------------------------------------------------
# AC-5 — Content-Length pre-check
# ---------------------------------------------------------------------------
async def test_post_rejects_oversize_via_content_length_413(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-5 — A request with Content-Length > MAX_UPLOAD_MB
    is rejected 413 AUDIO_TOO_LARGE BEFORE the orchestrator is invoked
    (cheap pre-check; saves ffmpeg startup cost on hostile uploads).
    """
    from transcription_api.config import settings

    user, plaintext = await _seed_user_with_bearer(session, email_suffix="size")
    too_big = settings.max_upload_mb * 1024 * 1024 + 1

    # We DO NOT actually upload that many bytes — just lie about Content-Length.
    # A faithful client would send the body; a hostile one might. Either way
    # the pre-check fires first.
    with patch(
        "transcription_api.api.transcriptions.orchestrate",
        new=AsyncMock(side_effect=AssertionError("orchestrate must NOT be called")),
    ):
        r = await client.post(
            "/api/transcriptions",
            content=b"",  # empty body, but we'll inject a fake Content-Length
            headers={
                "authorization": f"Bearer {plaintext}",
                "content-length": str(too_big),
                "content-type": "multipart/form-data; boundary=fake",
            },
        )

    assert r.status_code == 413
    assert r.json()["detail"]["error_code"] == "AUDIO_TOO_LARGE"
    assert r.json()["detail"]["max_mb"] == settings.max_upload_mb


# ---------------------------------------------------------------------------
# AC-15 — MODELS_NOT_LOADED short-circuit
# ---------------------------------------------------------------------------
async def test_post_returns_503_when_pyannote_failed_to_load(
    app_with_models_ready, session
):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-15 — Given app.state.pyannote_status='error' (the
    lifespan caught a PyannoteLoadError without aborting startup),
    When POST runs, Then the response is 503 MODELS_NOT_LOADED with
    the discriminator from app.state.pyannote_detail propagated as
    `detail` so the operator/client knows which HF condition failed.
    """
    user, plaintext = await _seed_user_with_bearer(session, email_suffix="nopa")

    # Force the failure mode after lifespan completed.
    app_with_models_ready.state.pyannote_status = "error"
    app_with_models_ready.state.pyannote_detail = "hf_token_invalid"

    transport = ASGITransport(app=app_with_models_ready)
    async with AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        with patch(
            "transcription_api.api.transcriptions.orchestrate",
            new=AsyncMock(side_effect=AssertionError("must NOT be called")),
        ):
            r = await c.post(
                "/api/transcriptions",
                files={"file": ("x.mp3", io.BytesIO(b"ID3"), "audio/mpeg")},
                headers={"authorization": f"Bearer {plaintext}"},
            )

    assert r.status_code == 503
    body = r.json()
    assert body["detail"]["error_code"] == "MODELS_NOT_LOADED"
    assert body["detail"]["detail"] == "hf_token_invalid"


async def test_post_returns_503_when_whisper_failed_to_load(
    app_with_models_ready, session
):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-15 — Same contract for whisper as for pyannote.
    The reason field cites which model failed.
    """
    user, plaintext = await _seed_user_with_bearer(session, email_suffix="nowh")

    app_with_models_ready.state.whisper_status = "error"
    app_with_models_ready.state.whisper_detail = "CUDA driver not available"

    transport = ASGITransport(app=app_with_models_ready)
    async with AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        r = await c.post(
            "/api/transcriptions",
            files={"file": ("x.mp3", io.BytesIO(b"ID3"), "audio/mpeg")},
            headers={"authorization": f"Bearer {plaintext}"},
        )

    assert r.status_code == 503
    body = r.json()
    assert body["detail"]["error_code"] == "MODELS_NOT_LOADED"
    assert "whisper" in body["detail"]["reason"].lower()


# ---------------------------------------------------------------------------
# AC-13 + AC-8 — GET own + cross-user 404
# ---------------------------------------------------------------------------
async def test_get_returns_full_result_for_owner(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-13 — Owner reads their transcription -> 200 with the
    same shape as POST. The listener AND-injects user_id == owner.id;
    the row matches; row -> response.
    """
    user, plaintext = await _seed_user_with_bearer(session, email_suffix="own")
    row = await make_transcription(
        session, user_id=user.id, audio_hash="own-hash" + "0" * 56
    )
    await session.commit()

    r = await client.get(
        f"/api/transcriptions/{row.id}",
        headers={"authorization": f"Bearer {plaintext}"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcription_id"] == str(row.id)
    assert body["audio_hash"] == row.audio_hash
    assert body["language"] == row.language


async def test_get_cross_user_returns_404_no_existence_leak(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-8 — User B with B's own bearer requests user A's
    transcription_id -> 404 TRANSCRIPTION_NOT_FOUND. SAME shape as a
    fully-nonexistent id (no existence leak). The listener filters
    A's row out of B's session — scalar_one_or_none returns None.
    """
    alice = await make_user(session, email="alice-cross@x")
    alice_row = await make_transcription(
        session, user_id=alice.id, audio_hash="alice-hash" + "0" * 54
    )
    bob, bob_pt = await _seed_user_with_bearer(session, email_suffix="cross-bob")
    await session.commit()

    r = await client.get(
        f"/api/transcriptions/{alice_row.id}",
        headers={"authorization": f"Bearer {bob_pt}"},
    )

    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "TRANSCRIPTION_NOT_FOUND"


async def test_get_nonexistent_id_returns_404_same_shape(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-8 (defensive) — A purely-nonexistent UUID returns
    the SAME 404 body as a cross-user attempt. This lets the test
    confirm the existence leak is closed by the listener: no extra
    branch in the route distinguishes the two cases.
    """
    user, plaintext = await _seed_user_with_bearer(session, email_suffix="ghost")

    r = await client.get(
        f"/api/transcriptions/{uuid.uuid4()}",
        headers={"authorization": f"Bearer {plaintext}"},
    )

    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "TRANSCRIPTION_NOT_FOUND"


async def test_get_without_bearer_returns_401(client):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-3 (extends to GET) — No bearer -> 401, same as POST.
    """
    r = await client.get(f"/api/transcriptions/{uuid.uuid4()}")
    assert r.status_code == 401
    assert r.json()["detail"]["error_code"] == "AUTH_NOT_AUTHENTICATED"
