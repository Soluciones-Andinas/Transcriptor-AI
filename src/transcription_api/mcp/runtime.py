"""Runtime model handles + readiness status, armed per request.

Spec: SPEC-capa4-mcp-v1
Covers (G12 — review-fixes Tier 3):
- The lifespan loads ``whisper_model`` / ``pyannote_pipeline`` once at
  startup and stores them on ``app.state`` of the *main* FastAPI app.
  MCP tools running inside the mounted sub-app cannot reach
  ``request.app.state`` of the main app directly — historically each
  tool did ``from ...main import app`` at call time as the documented
  workaround for the main <-> mcp circular import.
- This module replaces that pattern with ContextVars armed by the
  bearer middleware once per request. The lazy ``main.app`` import
  now lives in exactly one place (the middleware) and tools read
  through accessors that have no module-level dependency on ``main``.

The ContextVars default to None so a tool that runs OUTSIDE a request
(e.g., direct invocation in a unit test that did not arm the runtime)
sees None and can either fail-fast or fall back. Tests use
``arm_runtime_from_state`` directly to populate the ContextVars.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

# None until armed; tools that read these BEFORE the middleware ran
# (e.g., a misconfigured request flow) get None and should treat it as
# MODELS_NOT_LOADED. Tests arm them via ``arm_runtime_from_state``.
_current_whisper_model: ContextVar[Any] = ContextVar(
    "_current_whisper_model", default=None
)
_current_pyannote_pipeline: ContextVar[Any] = ContextVar(
    "_current_pyannote_pipeline", default=None
)
_current_models_status: ContextVar[dict[str, Any] | None] = ContextVar(
    "_current_models_status", default=None
)


def arm_runtime_from_state(app_state: Any) -> None:
    """Snapshot the lifespan-armed model state into ContextVars.

    Called by the bearer middleware on each request after a successful
    auth, with ``app_state`` being the MAIN FastAPI app's
    ``app.state`` (where the lifespan runs). Snapshotting via
    ContextVars decouples the tool layer from the main module.
    """
    _current_whisper_model.set(getattr(app_state, "whisper_model", None))
    _current_pyannote_pipeline.set(
        getattr(app_state, "pyannote_pipeline", None)
    )
    _current_models_status.set(
        {
            "whisper_status": getattr(app_state, "whisper_status", "unknown"),
            "pyannote_status": getattr(app_state, "pyannote_status", "unknown"),
            "whisper_detail": getattr(app_state, "whisper_detail", None),
            "pyannote_detail": getattr(app_state, "pyannote_detail", None),
        }
    )


def get_runtime_models() -> tuple[Any, Any]:
    """Return ``(whisper_model, pyannote_pipeline)`` armed by the middleware."""
    return _current_whisper_model.get(), _current_pyannote_pipeline.get()


def get_runtime_status() -> dict[str, Any] | None:
    """Return the readiness status snapshot armed by the middleware.

    Shape: ``{"whisper_status", "pyannote_status", "whisper_detail",
    "pyannote_detail"}``. ``None`` when the middleware never ran (e.g.,
    a test that imports the tool directly without arming the context).
    """
    return _current_models_status.get()
