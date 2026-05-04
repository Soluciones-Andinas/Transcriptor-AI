"""FastAPI app — Capa 1 (Postgres + ORM) integrated.

Spec: SPEC-capa1-postgres-orm-v1, AC-8 + AC-9 + ERR-1.

Lifespan owns the AsyncEngine: it stores a reference on `app.state.engine`
and disposes the pool at shutdown. `GET /health` pings the DB with
`SELECT 1` and reports `db_reachable`. Crucially, /health NEVER raises
when the DB is down — it returns 200 with degraded payload so Docker's
restart policy does not loop the container.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel
from sqlalchemy import text

from .config import settings

logger = logging.getLogger("transcription_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hooks.

    Engine is imported lazily here so that test monkeypatching of
    `transcription_api.db.session.engine` is honored.
    """
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

    # DATA_DIR/{models,cache} (RF-CACHE-01 step 2).
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "data_dirs_ready models_dir=%s cache_dir=%s",
        settings.models_dir,
        settings.cache_dir,
    )

    # Capa 1 — bind the engine to app.state. Lazy import so tests can swap
    # `transcription_api.db.session.engine` before lifespan runs.
    from . import db as _db
    app.state.engine = _db.engine
    logger.info("db_engine_bound url_host=%s", _db.engine.url.host)

    try:
        yield
    finally:
        # ERR-1: even if the user kills the container mid-startup, dispose
        # never raises — release the pool best-effort.
        try:
            await _db.engine.dispose()
            logger.info("db_engine_disposed")
        except Exception:  # noqa: BLE001
            logger.warning("db_engine_dispose_failed", exc_info=True)
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
    db_reachable: bool


async def _ping_db(engine) -> bool:
    """SELECT 1 against the DB. Returns False on any failure (logged)."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — /health must never raise (ERR-1)
        logger.warning("health_db_ping_failed", exc_info=True)
        return False


@app.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Reports liveness and runtime invariants. Never raises (ERR-1).

    Used by Docker healthcheck and by clients to verify GPU + DB before
    submitting requests.
    """
    gpu_available = False
    gpu_name: str | None = None
    vram_total_mb: int | None = None
    vram_free_mb: int | None = None
    cuda_version: str | None = None

    # torch is an optional dep; probe defensively so /health does not 500.
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

    # Disco escribible (RF-TRX-06, RF-CACHE-01).
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

    db_reachable = await _ping_db(request.app.state.engine)

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
        db_reachable=db_reachable,
    )
