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


# ---------------------------------------------------------------------------
# Capa 3 review fixes — Group 3 HTTP E2E coverage gaps
# (CR-5, H-6, H-7, H-8, H-9 from the multi-agent review).
# ---------------------------------------------------------------------------

# CR-5 / AC-2 — cache_hit metadata propagates through the HTTP layer.
async def test_post_cache_hit_metadata_propagates_through_api(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-2 (HTTP layer) — When the orchestrator returns
    ``metadata.cache_hit: True`` (per-user cache hit, ALT-1), the API
    response body MUST surface the same flag without transformation.

    Capa 3 review CR-5: the prior test suite verified cache_hit at the
    orchestrator level only ('transitive coverage'). A future API
    refactor that strips or renames metadata fields would not be
    detected. This test adds the structural assertion at the HTTP seam.
    """
    user, plaintext = await _seed_user_with_bearer(session, email_suffix="cachehit")
    transcription_id = uuid.uuid4()
    expected = _orchestrator_result(
        transcription_id=transcription_id, audio_hash="c" * 64
    )
    expected["metadata"]["cache_hit"] = True  # simulate ALT-1 hit

    with patch(
        "transcription_api.api.transcriptions.orchestrate",
        new=AsyncMock(return_value=expected),
    ):
        r = await client.post(
            "/api/transcriptions",
            files={"file": ("again.mp3", io.BytesIO(b"ID3" + b"\x00" * 32), "audio/mpeg")},
            headers={"authorization": f"Bearer {plaintext}"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["metadata"]["cache_hit"] is True


# CR-5 / AC-5 — streaming cap kicks in when Content-Length lies.
async def test_post_streaming_cap_rejects_oversize_when_content_length_lies(
    client, session
):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-5 — Even when Content-Length is missing or understates
    the body, the streaming read MUST cap at MAX_UPLOAD_MB and return
    413 AUDIO_TOO_LARGE without invoking the orchestrator.

    Capa 3 review CR-5: the existing AC-5 test only exercises the
    Content-Length pre-check path. This test removes the header (chunked
    transfer / lying client) and sends a body that exceeds the cap. The
    streaming read MUST detect the overflow and reject before orchestrate.
    """
    from transcription_api.config import settings

    user, plaintext = await _seed_user_with_bearer(session, email_suffix="oversize")
    # MAX_UPLOAD_MB is 500 by default → too big to allocate in tests.
    # Use a small body but monkey-patch settings.max_upload_mb to a tiny
    # threshold the test body easily exceeds.
    big_body = b"ID3" + b"\xab" * (200 * 1024)  # ~200 KB

    orchestrate_mock = AsyncMock()
    with patch.object(settings, "max_upload_mb", 0.1), patch(  # 100 KB cap
        "transcription_api.api.transcriptions.orchestrate", new=orchestrate_mock
    ):
        # Strip Content-Length by streaming via files= (httpx will add it
        # but the streaming-cap also runs regardless of Content-Length).
        r = await client.post(
            "/api/transcriptions",
            files={"file": ("oversize.mp3", io.BytesIO(big_body), "audio/mpeg")},
            headers={"authorization": f"Bearer {plaintext}"},
        )

    assert r.status_code == 413, r.text
    assert r.json()["detail"]["error_code"] == "AUDIO_TOO_LARGE"
    # Critical: orchestrate must NOT have been invoked — the cap is a
    # pre-orchestrate gate.
    orchestrate_mock.assert_not_called()


# H-6 / AC-12 — two users POSTing the same content each get their own row.
async def test_post_per_user_isolation_via_http(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-12 (HTTP layer) — Different users uploading the SAME
    audio_hash each invoke the orchestrator independently and obtain
    separate transcription_ids. The cache is per-user (D-027), so no
    cross-user leak via a 'shared cache hit' shortcut.

    Capa 3 review H-6: orchestrator-level test exists; HTTP-level was
    missing.
    """
    alice, alice_pt = await _seed_user_with_bearer(session, email_suffix="alice-iso")
    bob, bob_pt = await _seed_user_with_bearer(session, email_suffix="bob-iso")

    audio_hash = "abcdef" * 10 + "abcd"  # 64-char identical hash for both
    alice_tid = uuid.uuid4()
    bob_tid = uuid.uuid4()
    file_payload = (
        "shared.mp3",
        io.BytesIO(b"ID3" + b"\xcc" * 32),
        "audio/mpeg",
    )

    orchestrate_mock = AsyncMock(
        side_effect=[
            _orchestrator_result(transcription_id=alice_tid, audio_hash=audio_hash),
            _orchestrator_result(transcription_id=bob_tid, audio_hash=audio_hash),
        ]
    )

    with patch(
        "transcription_api.api.transcriptions.orchestrate", new=orchestrate_mock
    ):
        r_alice = await client.post(
            "/api/transcriptions",
            files={"file": file_payload},
            headers={"authorization": f"Bearer {alice_pt}"},
        )
        # Re-create the file payload — BytesIO is consumed.
        file_payload2 = (
            "shared.mp3",
            io.BytesIO(b"ID3" + b"\xcc" * 32),
            "audio/mpeg",
        )
        r_bob = await client.post(
            "/api/transcriptions",
            files={"file": file_payload2},
            headers={"authorization": f"Bearer {bob_pt}"},
        )

    assert r_alice.status_code == 200, r_alice.text
    assert r_bob.status_code == 200, r_bob.text
    assert r_alice.json()["transcription_id"] != r_bob.json()["transcription_id"]
    # Both users hit orchestrate (per-user cache; no shared shortcut).
    assert orchestrate_mock.call_count == 2
    # Verify each call received the correct user_id.
    user_ids_passed = {call.kwargs["user_id"] for call in orchestrate_mock.call_args_list}
    assert user_ids_passed == {alice.id, bob.id}


# H-6 / ALT-2 — silent audio E2E: text_content="" + silent_audio:true.
async def test_post_silent_audio_response_shape(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: ALT-2 (HTTP layer) — When the orchestrator detects empty
    STT segments (silent audio), the response body has text_content="",
    num_speakers=0, segments=[], and metadata.silent_audio: true.

    Capa 3 review H-6: orchestrator-level test exists; HTTP shape gap.
    """
    user, plaintext = await _seed_user_with_bearer(session, email_suffix="silent")
    transcription_id = uuid.uuid4()
    expected = {
        "transcription_id": transcription_id,
        "audio_hash": "s" * 64,
        "language": "es",
        "duration_seconds": 5.0,
        "num_speakers": 0,
        "text_content": "",
        "segments": [],
        "metadata": {
            "model": "large-v3",
            "diarizer": "pyannote/speaker-diarization-3.1",
            "compute_type": "int8_float16",
            "cache_hit": False,
            "silent_audio": True,
        },
    }

    with patch(
        "transcription_api.api.transcriptions.orchestrate",
        new=AsyncMock(return_value=expected),
    ):
        r = await client.post(
            "/api/transcriptions",
            files={"file": ("silence.mp3", io.BytesIO(b"ID3" + b"\x00" * 32), "audio/mpeg")},
            headers={"authorization": f"Bearer {plaintext}"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text_content"] == ""
    assert body["num_speakers"] == 0
    assert body["segments"] == []
    assert body["metadata"]["silent_audio"] is True


# H-6 + H-7 / ALT-3 — max_speakers reaches orchestrator via call_args.
async def test_post_forwards_max_speakers_kwarg_to_orchestrate(client, session):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: ALT-3 (HTTP layer) — The form field ``max_speakers``
    propagates to the orchestrator's ``max_speakers`` kwarg verbatim.
    Without this, the cap-by-relabel logic in diarize is unreachable
    from the HTTP surface.

    Capa 3 review H-7: existing tests mock orchestrate but don't verify
    the kwargs that reached it. This test adds the call_args assertion.
    """
    user, plaintext = await _seed_user_with_bearer(session, email_suffix="capspeakers")
    transcription_id = uuid.uuid4()
    expected = _orchestrator_result(
        transcription_id=transcription_id, audio_hash="m" * 64
    )

    orchestrate_mock = AsyncMock(return_value=expected)
    with patch(
        "transcription_api.api.transcriptions.orchestrate", new=orchestrate_mock
    ):
        r = await client.post(
            "/api/transcriptions",
            files={"file": ("capped.mp3", io.BytesIO(b"ID3" + b"\x00" * 32), "audio/mpeg")},
            data={
                "language": "es",
                "num_speakers": 4,
                "min_speakers": 2,
                "max_speakers": 2,
            },
            headers={"authorization": f"Bearer {plaintext}"},
        )

    assert r.status_code == 200, r.text
    orchestrate_mock.assert_called_once()
    call_kwargs = orchestrate_mock.call_args.kwargs
    assert call_kwargs["max_speakers"] == 2
    assert call_kwargs["min_speakers"] == 2
    assert call_kwargs["num_speakers"] == 4
    assert call_kwargs["language"] == "es"


# H-8 / AC-15 — pyannote_detail surfaces in the 503 body verbatim.
async def test_post_503_pyannote_detail_propagates_specific_string(
    app_with_models_ready, client, session
):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-15 — When pyannote loading failed with a specific
    classified detail (``hf_token_invalid`` etc.), the 503 response
    body MUST surface that detail in ``detail.detail`` so the operator
    knows which HF condition triggered the degraded state.

    Capa 3 review H-8: the existing AC-15 test asserts the error_code
    + ``"pyannote"`` in reason but not the detail field. This test adds
    the specific-detail-string assertion.
    """
    user, plaintext = await _seed_user_with_bearer(session, email_suffix="hferror")
    # Override the patched-ready state to inject a specific pyannote error.
    app_with_models_ready.state.pyannote_status = "error"
    app_with_models_ready.state.pyannote_detail = "hf_token_invalid"

    r = await client.post(
        "/api/transcriptions",
        files={"file": ("x.mp3", io.BytesIO(b"ID3"), "audio/mpeg")},
        headers={"authorization": f"Bearer {plaintext}"},
    )

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["detail"]["error_code"] == "MODELS_NOT_LOADED"
    assert body["detail"]["detail"] == "hf_token_invalid", body
    # Restore for any subsequent test in the same fixture.
    app_with_models_ready.state.pyannote_status = "ready"
    app_with_models_ready.state.pyannote_detail = None


# ---------------------------------------------------------------------------
# AC-16 (Capa 4) — Legacy endpoint WARN log on invocation
# (OpenAPI deprecation flag is asserted in test_legacy_deprecation.py — that
# assertion is decorator-time and does not need testcontainers / Docker.)
# ---------------------------------------------------------------------------
async def test_post_transcriptions_emits_legacy_warn_on_invocation(
    client, session, caplog
):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-16 — When the legacy endpoint is invoked, the handler
    emits a WARN log with the structured fields
    ``legacy_endpoint_invoked deprecated_endpoint=POST_/api/transcriptions
    removal_target=Capa5``. The endpoint still returns the orchestrator
    result (deprecation is signal, not break) — Capa 5 will remove it.
    """
    import logging

    user, plaintext = await _seed_user_with_bearer(session, email_suffix="legacy")
    transcription_id = uuid.uuid4()
    expected = _orchestrator_result(
        transcription_id=transcription_id, audio_hash="c" * 64
    )

    with caplog.at_level(logging.WARNING, logger="transcription_api.api.transcriptions"):
        with patch(
            "transcription_api.api.transcriptions.orchestrate",
            new=AsyncMock(return_value=expected),
        ):
            r = await client.post(
                "/api/transcriptions",
                files={"file": ("x.mp3", io.BytesIO(b"ID3" + b"\x00" * 32), "audio/mpeg")},
                data={"language": "es"},
                headers={"authorization": f"Bearer {plaintext}"},
            )

    assert r.status_code == 200, r.text
    warns = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING and "legacy_endpoint_invoked" in rec.getMessage()
    ]
    assert warns, (
        "expected a WARN with 'legacy_endpoint_invoked' on legacy POST; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    msg = warns[0].getMessage()
    assert "deprecated_endpoint=POST_/api/transcriptions" in msg
    assert "removal_target=Capa5" in msg


# H-9 / AC-9 — /health "loading" default surfaces when state attrs missing.
async def test_health_loading_default_when_state_attrs_unset(
    app_with_models_ready, client
):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-9 — Before lifespan completes (or in degraded boot
    states), ``/health.models.{whisper,pyannote}`` MUST default to
    ``"loading"`` rather than crash on missing state attrs.

    Capa 3 review H-9: the existing AC-9 test asserts the post-lifespan
    "ready" state. This test asserts the pre-/missing-attr fallback by
    transiently clearing state and hitting /health.
    """
    state = app_with_models_ready.state
    # Cache and restore so we don't break sibling tests sharing the fixture.
    saved_w_status = getattr(state, "whisper_status", "ready")
    saved_p_status = getattr(state, "pyannote_status", "ready")
    try:
        delattr(state, "whisper_status")
        delattr(state, "pyannote_status")

        r = await client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["models"]["whisper"] == "loading"
        assert body["models"]["pyannote"] == "loading"
    finally:
        state.whisper_status = saved_w_status
        state.pyannote_status = saved_p_status
