"""Runtime context arming via ContextVars (G12 review-fix).

Spec: SPEC-capa4-mcp-v1
Covers (G12 — review-fixes Tier 3):
- ``mcp/tools/start.py`` previously did ``from ...main import app`` at
  call time to reach the lifespan-armed ``app.state.whisper_model`` /
  ``app.state.pyannote_pipeline``. The lazy import was the documented
  workaround for the main <-> mcp circular dependency, but it leaked
  the dependency into every tool that needs runtime state. G12 moves
  the import into the bearer middleware, which arms ContextVars on
  request entry; tools read the values via ``runtime.get_runtime_*``.

This file pins both invariants:
1. ``tools/start.py`` source must NOT contain ``from ...main import``
   (the import is centralized in middleware).
2. The runtime accessors return the values armed by the middleware.
"""
from __future__ import annotations

from pathlib import Path


def test_tools_start_does_not_lazy_import_main_app() -> None:
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: G12 review-fix — ``tools/start.py`` must read runtime
    handles via ``mcp.runtime`` accessors, NOT via a lazy
    ``from ...main import app`` inside the function body. Pins the
    cycle-elimination contract: a future regression that brings back
    the lazy import would break this assertion immediately.
    """
    src_path = Path(__file__).resolve().parents[3] / (
        "src/transcription_api/mcp/tools/start.py"
    )
    src = src_path.read_text(encoding="utf-8")
    assert "from ...main import" not in src, (
        f"tools/start.py reintroduced lazy main import:\n{src}"
    )


def test_runtime_module_exposes_arm_and_get_helpers() -> None:
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: G12 review-fix — ``mcp.runtime`` must expose the public
    helpers ``arm_runtime_from_state``, ``get_runtime_models``, and
    ``get_runtime_status`` so middleware + tools agree on the contract.
    """
    from transcription_api.mcp import runtime

    assert callable(runtime.arm_runtime_from_state)
    assert callable(runtime.get_runtime_models)
    assert callable(runtime.get_runtime_status)
