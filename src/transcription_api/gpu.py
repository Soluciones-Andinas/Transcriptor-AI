"""Hardware accelerator detection.

Single source of truth for "what GPU/accelerator is available right now":
- NVIDIA CUDA (production rig — RTX 5060 Ti 16 GB).
- Apple Silicon MPS (developer Macs).
- CPU fallback (CI runners, no-torch dev installs).

Used by:
- `GET /health` to report runtime capability to operators.
- `@pytest.mark.requires_gpu` tests to skip when no accelerator is present.
- Future Capa 4 pipeline to choose the right device for WhisperX.

`torch` is intentionally an optional dependency (`pip install -e ".[pipeline]"`).
This module degrades gracefully when torch is missing — it returns CPU info
without raising. Never import torch at module top-level here, or `transcription_api`
becomes unimportable on machines without torch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Backend = Literal["cuda", "mps", "cpu"]


@dataclass(frozen=True)
class AcceleratorInfo:
    """Snapshot of the active accelerator. All fields populated regardless of backend."""

    backend: Backend
    available: bool
    device_name: str | None
    vram_total_mb: int | None
    vram_free_mb: int | None
    cuda_version: str | None  # only set when backend == "cuda"


_CPU_INFO = AcceleratorInfo(
    backend="cpu",
    available=False,
    device_name=None,
    vram_total_mb=None,
    vram_free_mb=None,
    cuda_version=None,
)


def detect_accelerator() -> AcceleratorInfo:
    """Probe the runtime hardware. Pure function — safe to call from /health.

    Resolution order:
    1. NVIDIA CUDA (`torch.cuda.is_available()`).
    2. Apple Silicon MPS (`torch.backends.mps.is_available()` and `is_built()`).
    3. CPU (always returned when torch is missing or no accelerator is found).
    """
    try:
        import torch  # type: ignore[import-not-found]
    except ImportError:
        return _CPU_INFO

    # CUDA — production target. NVIDIA RTX 5060 Ti, 16 GB VRAM.
    if torch.cuda.is_available():
        free_bytes, total_bytes = torch.cuda.mem_get_info(0)
        return AcceleratorInfo(
            backend="cuda",
            available=True,
            device_name=torch.cuda.get_device_name(0),
            vram_total_mb=total_bytes // (1024 * 1024),
            vram_free_mb=free_bytes // (1024 * 1024),
            cuda_version=torch.version.cuda,
        )

    # MPS — Apple Silicon (dev machines). Memory introspection is not available
    # on MPS the way mem_get_info works on CUDA, so vram_* stay None.
    mps_backend = getattr(torch.backends, "mps", None)
    if mps_backend is not None and mps_backend.is_available() and mps_backend.is_built():
        return AcceleratorInfo(
            backend="mps",
            available=True,
            device_name="Apple Silicon GPU (MPS)",
            vram_total_mb=None,
            vram_free_mb=None,
            cuda_version=None,
        )

    return _CPU_INFO
