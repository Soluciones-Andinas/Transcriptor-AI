"""Centralized readiness gate for whisper / pyannote models.

Spec: SPEC-capa4-mcp-v1
Covers (G2 — review-fixes Tier 1):
- Both ``POST /api/transcriptions`` and the MCP tool
  ``start_transcription`` need an identical "are the lifespan-loaded
  models ready?" check before launching the pipeline. Two divergent
  inline implementations existed, so a fix to one (e.g., a new
  detail field) silently failed to propagate to the other. The
  helper consolidates the contract: same precedence, same fields,
  same precedence-of-failing-model semantics.

The helper is pure with respect to ``app_state`` — it reads
``whisper_status`` / ``pyannote_status`` plus the optional
``*_detail`` keys, and returns a frozen dataclass. Call sites
translate the result to either a 503 ``JSONResponse`` (REST) or
an ``McpError`` (MCP); they no longer duplicate the precedence logic.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReadinessResult:
    """Outcome of inspecting the lifespan model state.

    Fields:
        ready: True iff both models are in ``"ready"`` state.
        failing_model: ``"whisper"``, ``"pyannote"``, or ``None`` when
            ``ready=True``. Whisper takes precedence on a double-fault
            (mirrors the prior REST/MCP behavior so error messages
            stay stable across the cutover).
        detail: structured detail string from the lifespan loader for
            the failing model (e.g., a typed ``WhisperLoadError.detail``
            discriminator) — None when the loader did not surface one.
    """

    ready: bool
    failing_model: str | None
    detail: str | None


def check_models_ready(app_state) -> ReadinessResult:
    """Inspect lifespan-armed ``app.state`` for whisper + pyannote readiness.

    Raises:
        RuntimeError: if ``whisper_status`` is missing entirely. That
            signals the FastAPI lifespan never ran (or crashed before
            arming state). Surfaces louder than an ambiguous "loading"
            so the operator catches the misconfiguration immediately.
    """
    if not hasattr(app_state, "whisper_status"):
        raise RuntimeError(
            "LIFESPAN_DID_NOT_ARM_STATE: app.state.whisper_status missing. "
            "This indicates the FastAPI lifespan never ran or crashed early."
        )
    whisper = app_state.whisper_status
    pyannote = getattr(app_state, "pyannote_status", "loading")
    if whisper == "ready" and pyannote == "ready":
        return ReadinessResult(ready=True, failing_model=None, detail=None)
    if whisper != "ready":
        return ReadinessResult(
            ready=False,
            failing_model="whisper",
            detail=getattr(app_state, "whisper_detail", None),
        )
    return ReadinessResult(
        ready=False,
        failing_model="pyannote",
        detail=getattr(app_state, "pyannote_detail", None),
    )
