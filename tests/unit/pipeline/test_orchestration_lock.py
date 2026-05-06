"""Unit tests for _OrchestrationLock.

Spec: SPEC-capa3-pipeline-v1
Capa 3 review CR-1 + CR-2 — covers the owner-task tracker introduced
to fix BPO-31647 (asyncio.wait_for(lock.acquire(), timeout=...) can
raise TimeoutError after acquire succeeded).

These tests target the pure-Python lock class directly, no Postgres /
GPU / async-DB dependencies — runs natively on CPU dev machines.
"""
from __future__ import annotations

import asyncio

import pytest


async def test_held_by_current_task_returns_false_when_not_held():
    """Default state: nobody holds the lock → False."""
    from transcription_api.pipeline.orchestrator import _OrchestrationLock

    lock = _OrchestrationLock()
    assert lock.held_by_current_task() is False
    assert lock.locked() is False


async def test_held_by_current_task_returns_true_after_acquire():
    """Acquire by current task → owner_task is current_task → True."""
    from transcription_api.pipeline.orchestrator import _OrchestrationLock

    lock = _OrchestrationLock()
    await lock.acquire()
    try:
        assert lock.held_by_current_task() is True
        assert lock.locked() is True
    finally:
        lock.release()


async def test_held_by_current_task_returns_false_for_other_task():
    """Lock held by task A → task B's held_by_current_task() is False.

    The whole point of CR-1's fix: task B cannot release task A's hold
    via a defensive locked()-based check.
    """
    from transcription_api.pipeline.orchestrator import _OrchestrationLock

    lock = _OrchestrationLock()
    holder_done = asyncio.Event()
    can_release = asyncio.Event()

    async def holder():
        await lock.acquire()
        try:
            holder_done.set()
            await can_release.wait()
        finally:
            lock.release()

    holder_task = asyncio.create_task(holder())
    await holder_done.wait()

    # We are NOT the holder. locked() is True but held_by_current_task() False.
    assert lock.locked() is True
    assert lock.held_by_current_task() is False

    can_release.set()
    await holder_task


async def test_held_by_current_task_returns_false_after_release():
    """Release clears owner_task → held_by_current_task() is False."""
    from transcription_api.pipeline.orchestrator import _OrchestrationLock

    lock = _OrchestrationLock()
    await lock.acquire()
    lock.release()
    assert lock.held_by_current_task() is False
    assert lock.locked() is False


async def test_acquire_releases_internally_on_cancellation_after_acquire():
    """BPO-31647 inner case: cancellation lands during acquire's atomic step.

    We can't reliably trigger the exact race in a deterministic test, but
    we CAN verify that if a cancellation propagates AFTER the underlying
    asyncio.Lock acquired, the OrchestrationLock.acquire's except branch
    detects the locked() && owner_task is None case and releases.

    Test strategy: monkey-patch the inner asyncio.Lock to raise
    CancelledError synchronously after marking itself as acquired. Verify
    that after the OrchestrationLock.acquire raises, the inner lock is
    released and owner_task is None.
    """
    from transcription_api.pipeline.orchestrator import _OrchestrationLock

    lock = _OrchestrationLock()

    # Force the acquire path to: atomically mark as locked, then raise
    # CancelledError. This simulates the race window.
    original_acquire = lock._lock.acquire

    async def racing_acquire():
        # Acquire the underlying lock, then raise CancelledError synchronously.
        # This simulates the race window where the lock is HELD but the
        # cancellation prevents owner_task from being set.
        await original_acquire()
        raise asyncio.CancelledError()

    lock._lock.acquire = racing_acquire  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await lock.acquire()

    # The except branch of _OrchestrationLock.acquire should have detected
    # locked() && owner_task is None and released.
    assert lock.locked() is False, (
        "BPO-31647 inner mitigation failed: lock leaked after cancellation"
    )
    assert lock.held_by_current_task() is False
