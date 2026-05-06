"""Lifespan loads pipeline models; /health reports their state.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-9  — After the lifespan completes, /health.models.{whisper,pyannote}
  reports ``"ready"`` and the /health.pipeline block carries default
  ``lock_held=False``, ``active_job_id=None``, ``queue_depth=0`` (the lock
  is wired in Batch 5; defaults are surfaced now so the contract is stable).
- AC-15 — When pyannote fails to load (HF_TOKEN invalid, gated repo, etc.)
  the service stays UP, /health.status remains ``"ok"`` and
  /health.models.pyannote = ``"error"`` with the discriminator copied to
  /health.models.pyannote_detail (D-028: lazy + verbose).
- AC-9 (negative twin) — Whisper load failure (e.g. CUDA driver missing)
  surfaces as /health.models.whisper = ``"error"`` with whisper_detail.

The heavy ML loaders are patched at the module-attribute level so neither
GPU nor HuggingFace network access is required. The lifespan reads the
loaders via module-level attribute lookup (`pipeline.stt.load_whisper_model`,
`pipeline.diarize.load_pyannote_pipeline`) so `unittest.mock.patch` of those
attributes intercepts the call.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def stub_db_ping(monkeypatch):
    """Bypass /health's DB ping so these tests don't need a real Postgres.

    The Capa 3 health-models contract is orthogonal to db_reachable; stubbing
    the ping isolates the assertion to the new `models`/`pipeline` blocks.
    """
    async def _stub(_engine):
        return True

    monkeypatch.setattr("transcription_api.main._ping_db", _stub)


# ---------------------------------------------------------------------------
# AC-9 — happy path
# ---------------------------------------------------------------------------
async def test_health_reports_models_ready_after_lifespan(stub_db_ping):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-9 — Given the lifespan loads both models successfully,
    When GET /health, Then body.models.{whisper,pyannote} = "ready" and
    body.pipeline carries the default lock-held block.
    """
    from transcription_api.main import app

    fake_whisper = MagicMock(name="whisper_model")
    fake_pyannote = MagicMock(name="pyannote_pipeline")
    with patch(
        "transcription_api.pipeline.stt.load_whisper_model",
        return_value=fake_whisper,
    ), patch(
        "transcription_api.pipeline.diarize.load_pyannote_pipeline",
        return_value=fake_pyannote,
    ):
        async with LifespanManager(app):
            assert app.state.whisper_model is fake_whisper
            assert app.state.pyannote_pipeline is fake_pyannote
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://t"
            ) as client:
                r = await client.get("/health")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["models"]["whisper"] == "ready"
    assert body["models"]["pyannote"] == "ready"
    # Pipeline lock block defaults — Batch 5 will start mutating these.
    assert body["pipeline"]["lock_held"] is False
    assert body["pipeline"]["active_job_id"] is None
    assert body["pipeline"]["queue_depth"] == 0


# ---------------------------------------------------------------------------
# AC-15 — pyannote failure does not bring the service down
# ---------------------------------------------------------------------------
async def test_health_reports_pyannote_error_when_load_fails(stub_db_ping):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-15 — Given pyannote raises PyannoteLoadError during
    startup, When the service finishes booting, Then /health.status is "ok"
    (service stays UP), /health.models.pyannote = "error", and
    /health.models.pyannote_detail echoes the discriminator so operators
    know exactly which HF condition failed.
    """
    from transcription_api.main import app
    from transcription_api.pipeline.diarize import PyannoteLoadError

    with patch(
        "transcription_api.pipeline.stt.load_whisper_model",
        return_value=MagicMock(),
    ), patch(
        "transcription_api.pipeline.diarize.load_pyannote_pipeline",
        side_effect=PyannoteLoadError("hf_token_invalid", "401 Unauthorized"),
    ):
        async with LifespanManager(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://t"
            ) as client:
                r = await client.get("/health")

    assert r.status_code == 200, r.text
    body = r.json()
    # Service stays UP — the contract from D-028 / AC-15.
    assert body["status"] == "ok"
    assert body["models"]["whisper"] == "ready"
    assert body["models"]["pyannote"] == "error"
    assert body["models"]["pyannote_detail"] == "hf_token_invalid"


# ---------------------------------------------------------------------------
# AC-9 negative twin — Whisper failure also stays up
# ---------------------------------------------------------------------------
async def test_health_reports_whisper_error_when_load_fails(stub_db_ping):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-9 + H-5 — Given the inner whisperx loader raises a
    runtime error that the wrapper can classify (e.g. CUDA driver
    missing), When the service finishes booting, Then /health.status
    is "ok" and /health.models.whisper = "error" with whisper_detail
    set to a stable discriminator (here: ``cuda_unavailable``).

    Capa 3 review H-5: the previous variant patched
    ``load_whisper_model`` directly with a raw RuntimeError, which
    bypassed the classifier wrapper. The classifier IS the load-bearing
    feature for typed details, so we must exercise it end-to-end —
    patch the inner indirection ``_whisperx_load_model`` so the
    classifier runs.
    """
    from transcription_api.main import app

    with patch(
        "transcription_api.pipeline.stt._whisperx_load_model",
        side_effect=RuntimeError("CUDA driver not available; cuda is not available"),
    ), patch(
        "transcription_api.pipeline.diarize.load_pyannote_pipeline",
        return_value=MagicMock(),
    ):
        async with LifespanManager(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://t"
            ) as client:
                r = await client.get("/health")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["models"]["whisper"] == "error"
    # H-5: detail is a closed enum value, not raw exception text.
    assert body["models"]["whisper_detail"] == "cuda_unavailable"
    # Other model unaffected.
    assert body["models"]["pyannote"] == "ready"
