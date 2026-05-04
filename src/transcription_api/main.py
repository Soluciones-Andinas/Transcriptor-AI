"""FastAPI app — Fase 1 stub.

This module currently exposes only `GET /health` to validate the Docker infra
end-to-end (build, run, GPU pass-through, healthcheck). The full pipeline
(POST /transcribe, lock, normalizer, STT, diarization, cache) is implemented
in subsequent phases following the order documented in wiki/06_matriz_pruebas_RF.md.
"""
import logging
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from .config import settings

logger = logging.getLogger("transcription_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hooks. In Fase 2 this loads Whisper/pyannote into VRAM."""
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info(
        "service_starting data_dir=%s default_language=%s compute_type=%s",
        settings.data_dir,
        settings.default_language,
        settings.compute_type,
    )

    # Crear DATA_DIR/models y DATA_DIR/cache si no existen (RF-CACHE-01 step 2).
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "data_dirs_ready models_dir=%s cache_dir=%s",
        settings.models_dir,
        settings.cache_dir,
    )

    yield

    logger.info("service_stopping")


app = FastAPI(
    title="transcription-api",
    description="Self-hosted Spanish transcription + diarization for Sandinas",
    version="0.1.0",
    lifespan=lifespan,
)


class HealthResponse(BaseModel):
    """Response of `GET /health`. Exposes runtime state to the operator."""

    status: str
    version: str
    gpu_available: bool
    gpu_name: str | None
    vram_total_mb: int | None
    vram_free_mb: int | None
    cuda_version: str | None
    data_dir_writable: bool
    cache_entries: int


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Reports liveness and core runtime invariants.

    Used by Docker healthcheck and by clients to verify GPU availability
    before submitting long-running requests.
    """
    gpu_available = False
    gpu_name: str | None = None
    vram_total_mb: int | None = None
    vram_free_mb: int | None = None
    cuda_version: str | None = None

    # torch is an optional dependency in Fase 1 (only present once `[pipeline]` extra
    # is installed). We probe defensively so /health does not 500 in dev installs.
    try:
        import torch  # type: ignore[import-not-found]

        cuda_version = torch.version.cuda
        if torch.cuda.is_available():
            gpu_available = True
            gpu_name = torch.cuda.get_device_name(0)
            free_bytes, total_bytes = torch.cuda.mem_get_info(0)
            vram_total_mb = total_bytes // (1024 * 1024)
            vram_free_mb = free_bytes // (1024 * 1024)
    except ImportError:
        logger.debug("torch_not_installed pipeline_extra_missing")

    # Disco escribible: prerequisito de RF-TRX-06 y RF-CACHE-01.
    data_dir_writable = False
    try:
        probe = settings.data_dir / ".healthcheck-probe"
        probe.write_text("ok")
        probe.unlink()
        data_dir_writable = True
    except OSError as exc:
        logger.warning("data_dir_not_writable path=%s error=%s", settings.data_dir, exc)

    cache_entries = 0
    if settings.cache_dir.is_dir():
        cache_entries = sum(1 for _ in settings.cache_dir.iterdir())

    return HealthResponse(
        status="ok",
        version="0.1.0",
        gpu_available=gpu_available,
        gpu_name=gpu_name,
        vram_total_mb=vram_total_mb,
        vram_free_mb=vram_free_mb,
        cuda_version=cuda_version,
        data_dir_writable=data_dir_writable,
        cache_entries=cache_entries,
    )
