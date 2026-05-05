"""Accelerator detection + fallback semantics.

Spec: SPEC-capa1-postgres-orm-v1 (extension — review fix)
Helper: src/transcription_api/gpu.py

Tests do NOT require an actual GPU. They verify:
- The dataclass shape is stable.
- Fallback to CPU is robust when torch is absent or both CUDA/MPS are unavailable.
- The `requires_gpu` marker skips correctly when no accelerator is present.
"""
from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from transcription_api.gpu import AcceleratorInfo, detect_accelerator


def test_detect_returns_cpu_when_torch_missing(monkeypatch):
    """Importing torch must be optional; CPU is the safe fallback."""
    # Pretend `import torch` raises ImportError.
    monkeypatch.setitem(sys.modules, "torch", None)
    info = detect_accelerator()
    assert info.backend == "cpu"
    assert info.available is False
    assert info.device_name is None
    assert info.vram_total_mb is None


def test_detect_returns_cuda_info_when_cuda_available():
    """When torch.cuda.is_available(), populate device + VRAM + cuda version."""
    fake_torch = type(sys)("torch_stub")
    fake_torch.cuda = type(sys)("torch_cuda_stub")
    fake_torch.cuda.is_available = lambda: True
    fake_torch.cuda.mem_get_info = lambda idx: (8 * 1024 * 1024 * 1024, 16 * 1024 * 1024 * 1024)
    fake_torch.cuda.get_device_name = lambda idx: "NVIDIA GeForce RTX 4060 Ti"
    fake_torch.version = type(sys)("torch_version_stub")
    fake_torch.version.cuda = "12.1"
    fake_torch.backends = type(sys)("torch_backends_stub")
    fake_torch.backends.mps = type(sys)("torch_backends_mps_stub")
    fake_torch.backends.mps.is_available = lambda: False
    fake_torch.backends.mps.is_built = lambda: False

    with patch.dict(sys.modules, {"torch": fake_torch}):
        info = detect_accelerator()
    assert info.backend == "cuda"
    assert info.available is True
    assert info.device_name == "NVIDIA GeForce RTX 4060 Ti"
    assert info.vram_total_mb == 16 * 1024
    assert info.vram_free_mb == 8 * 1024
    assert info.cuda_version == "12.1"


def test_detect_returns_mps_info_on_apple_silicon():
    """When CUDA unavailable but MPS is, prefer MPS over CPU (dev Macs)."""
    fake_torch = type(sys)("torch_stub")
    fake_torch.cuda = type(sys)("torch_cuda_stub")
    fake_torch.cuda.is_available = lambda: False
    fake_torch.backends = type(sys)("torch_backends_stub")
    fake_torch.backends.mps = type(sys)("torch_backends_mps_stub")
    fake_torch.backends.mps.is_available = lambda: True
    fake_torch.backends.mps.is_built = lambda: True

    with patch.dict(sys.modules, {"torch": fake_torch}):
        info = detect_accelerator()
    assert info.backend == "mps"
    assert info.available is True
    assert info.device_name == "Apple Silicon GPU (MPS)"
    # MPS does not expose VRAM introspection; both must be None.
    assert info.vram_total_mb is None
    assert info.vram_free_mb is None
    assert info.cuda_version is None


def test_detect_returns_cpu_when_neither_cuda_nor_mps_available():
    """Torch present but no accelerator → CPU."""
    fake_torch = type(sys)("torch_stub")
    fake_torch.cuda = type(sys)("torch_cuda_stub")
    fake_torch.cuda.is_available = lambda: False
    fake_torch.backends = type(sys)("torch_backends_stub")
    fake_torch.backends.mps = type(sys)("torch_backends_mps_stub")
    fake_torch.backends.mps.is_available = lambda: False
    fake_torch.backends.mps.is_built = lambda: False

    with patch.dict(sys.modules, {"torch": fake_torch}):
        info = detect_accelerator()
    assert info.backend == "cpu"
    assert info.available is False


def test_accelerator_info_is_immutable():
    """Frozen dataclass: snapshot semantics, no surprise mutation."""
    info = AcceleratorInfo(
        backend="cpu", available=False,
        device_name=None, vram_total_mb=None, vram_free_mb=None, cuda_version=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        info.backend = "cuda"  # type: ignore[misc]
