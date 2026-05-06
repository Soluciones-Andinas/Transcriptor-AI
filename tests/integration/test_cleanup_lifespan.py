"""Cleanup task wired to the FastAPI lifespan.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-10 (lifespan integration) — Startup spawns a background task that
  periodically calls ``pipeline.cleanup.purge_expired``. Shutdown cancels
  the task and awaits it. Tests assert: (a) the task attribute lands on
  ``app.state.cleanup_task`` post-startup, (b) ``purge_expired`` is
  invoked at least once during the lifespan window, (c) the task is
  cancelled cleanly on shutdown.

The model loaders are mocked so the lifespan doesn't try to touch torch
or HuggingFace; only the cleanup wiring is under test here.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from asgi_lifespan import LifespanManager


@pytest.fixture
def patched_loaders():
    """Patch the model loaders so the lifespan doesn't try torch/pyannote."""
    with patch(
        "transcription_api.pipeline.stt.load_whisper_model",
        return_value=MagicMock(name="whisper"),
    ), patch(
        "transcription_api.pipeline.diarize.load_pyannote_pipeline",
        return_value=MagicMock(name="pyannote"),
    ):
        yield


async def test_lifespan_creates_cleanup_task(patched_loaders, monkeypatch):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-10 — Given a fresh app boot, When the lifespan
    completes startup, Then ``app.state.cleanup_task`` is an active
    ``asyncio.Task``. Verifies the wire: the task attribute exists and
    is not yet done.
    """
    # Force a tiny interval so the loop sleeps briefly + the task is
    # alive throughout the with-block (vs. the prod default of 3600s).
    monkeypatch.setattr(
        "transcription_api.config.settings.cache_cleanup_interval_seconds", 60
    )
    from transcription_api.main import app

    async with LifespanManager(app):
        task = getattr(app.state, "cleanup_task", None)
        assert isinstance(task, asyncio.Task)
        assert not task.done(), "cleanup task must be running while lifespan is up"


async def test_lifespan_invokes_purge_expired(patched_loaders, monkeypatch):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-10 — Given a tiny ``cache_cleanup_interval_seconds``,
    When the lifespan window stays open long enough for one tick, Then
    ``purge_expired`` is invoked at least once. Patches the helper so
    the test can assert the call without a real cache directory.
    """
    monkeypatch.setattr(
        "transcription_api.config.settings.cache_cleanup_interval_seconds",
        0.05,  # 50 ms — fast enough for the test, slow enough to exit cleanly
    )
    from transcription_api.main import app

    with patch(
        "transcription_api.main.purge_expired", return_value=0
    ) as purge_mock:
        async with LifespanManager(app):
            # Give the loop at least one tick.
            await asyncio.sleep(0.2)
        # By the time we exit the lifespan, purge_expired should have run.
        assert purge_mock.called, "cleanup loop must invoke purge_expired"


async def test_lifespan_cancels_cleanup_task_on_shutdown(
    patched_loaders, monkeypatch
):
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-10 — On shutdown, the cleanup task must be cancelled
    (not left orphaned). Awaiting a cancelled task in lifespan teardown
    must NOT propagate ``CancelledError`` out of the lifespan.
    """
    monkeypatch.setattr(
        "transcription_api.config.settings.cache_cleanup_interval_seconds", 60
    )
    from transcription_api.main import app

    async with LifespanManager(app):
        task = app.state.cleanup_task

    # Post-lifespan: the task is finished (cancelled or completed cleanly).
    # ``done()`` covers both the cancelled and completed cases.
    assert task.done(), "cleanup task must be settled after shutdown"
