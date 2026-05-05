"""FastAPI app — Capa 1 (Postgres + ORM) integrated.

Spec: SPEC-capa1-postgres-orm-v1, AC-8 + AC-9 + ERR-1 + ERR-2.

Lifespan owns the AsyncEngine on `app.state.engine` and disposes the pool
at shutdown. `GET /health` pings the DB and reports `db_reachable`. ERR-1:
/health NEVER raises when the DB is down — returns 200 with degraded
payload so Docker's restart policy does not loop the container. ERR-2:
pool exhaustion is mapped to HTTP 503 with `error_code: DB_POOL_EXHAUSTED`
via a global exception handler.

Logging is configured by uvicorn via `--log-config` (see entrypoint.sh).
This module never calls `logging.basicConfig` to avoid silent override.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError, TimeoutError as SAQueueTimeoutError

from .config import settings
from .gpu import AcceleratorInfo, detect_accelerator

logger = logging.getLogger("transcription_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hooks.

    Engine is imported lazily so test monkeypatching of
    `transcription_api.db.session.engine` is honored.
    """
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
    # `transcription_api.db.session.engine` before lifespan runs. Bind FIRST,
    # log SECOND, so a logging failure cannot leave app.state.engine unset
    # (which would cause /health to AttributeError and violate ERR-1).
    from . import db as _db
    app.state.engine = _db.engine
    host = getattr(_db.engine.url, "host", "<unknown>")
    logger.info("db_engine_bound url_host=%s", host)

    # S-1 (review fix): install the per-user scoping listener globally.
    # Idempotent; subsequent calls are no-ops. Capa 2 auth middleware will
    # populate `session.info["user_id"]` per request.
    _db.enable_per_user_scoping()
    logger.info("db_scoping_listener_enabled")

    try:
        yield
    finally:
        # ERR-1: dispose must not propagate; if the DB was already down,
        # closing dead connections itself raises. Log + swallow.
        try:
            await _db.engine.dispose()
            logger.info("db_engine_disposed")
        except Exception:
            logger.error("db_engine_dispose_failed error_id=DB_DISPOSE_FAILED", exc_info=True)
        logger.info("service_stopping")


app = FastAPI(
    title="transcription-api",
    description="Self-hosted Spanish transcription + diarization for Sandinas",
    version="0.1.0",
    lifespan=lifespan,
)


# Capa 2 — wire the auth module's router. Routes are progressively populated
# across batches B2-B5; B1 ships the smoke endpoint /auth/_ping so AC-1
# (test_auth_module_imports) verifies the wire.
from .auth import router as auth_router  # noqa: E402

app.include_router(auth_router)


# ---------------------------------------------------------------------------
# ERR-2 — pool exhaustion → HTTP 503 with stable error_code.
# ---------------------------------------------------------------------------
@app.exception_handler(SAQueueTimeoutError)
async def _pool_timeout_handler(request: Request, exc: SAQueueTimeoutError) -> JSONResponse:
    """Surface SQLAlchemy pool timeouts as HTTP 503 (ERR-2)."""
    logger.warning(
        "db_pool_exhausted error_id=DB_POOL_EXHAUSTED path=%s",
        request.url.path,
        exc_info=True,
    )
    return JSONResponse(
        status_code=503,
        content={
            "error_code": "DB_POOL_EXHAUSTED",
            "message": "Database connection pool exhausted; please retry shortly.",
        },
        headers={"Retry-After": "5"},
    )


@app.exception_handler(asyncio.TimeoutError)
async def _asyncio_timeout_handler(request: Request, exc: asyncio.TimeoutError) -> JSONResponse:
    """Generic async timeout — surfaced as 503. Most common source today is
    `pool.acquire()` while waiting on a free connection."""
    logger.warning(
        "request_timeout error_id=REQUEST_TIMEOUT path=%s",
        request.url.path,
        exc_info=True,
    )
    return JSONResponse(
        status_code=503,
        content={
            "error_code": "REQUEST_TIMEOUT",
            "message": "Request timed out; please retry shortly.",
        },
        headers={"Retry-After": "5"},
    )


# ---------------------------------------------------------------------------
# /health — never raises (ERR-1).
# ---------------------------------------------------------------------------
class HealthResponse(BaseModel):
    """Response of `GET /health`. Reports liveness + DB + accelerator state."""

    status: str
    version: str
    gpu_available: bool
    gpu_backend: str  # "cuda" | "mps" | "cpu"
    gpu_name: str | None
    vram_total_mb: int | None
    vram_free_mb: int | None
    cuda_version: str | None
    data_dir_writable: bool
    cache_entries: int
    db_reachable: bool


async def _ping_db(engine: Any) -> bool:
    """SELECT 1 against the DB. Returns False on legitimate connectivity failures.

    Programming errors (AttributeError, TypeError) propagate so they don't
    masquerade as "DB unreachable" and waste operator debugging time.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except (SQLAlchemyError, OSError, asyncio.TimeoutError):
        logger.warning("health_db_ping_failed error_id=DB_UNREACHABLE", exc_info=True)
        return False


def _probe_data_dir_writable() -> bool:
    """Touch and remove a probe file. Cleans up on partial failure."""
    probe = settings.data_dir / ".healthcheck-probe"
    try:
        probe.write_text("ok")
        return True
    except OSError as exc:
        logger.warning(
            "data_dir_not_writable path=%s error=%s error_id=DATA_DIR_RO",
            settings.data_dir,
            exc,
        )
        return False
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            # Probe leaks if write succeeded but unlink fails — log so it's not silent.
            logger.warning("data_dir_probe_leak path=%s error_id=DATA_DIR_PROBE_LEAK", probe)


def _accelerator_to_health(info: AcceleratorInfo) -> dict[str, Any]:
    return {
        "gpu_available": info.available,
        "gpu_backend": info.backend,
        "gpu_name": info.device_name,
        "vram_total_mb": info.vram_total_mb,
        "vram_free_mb": info.vram_free_mb,
        "cuda_version": info.cuda_version,
    }


@app.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Liveness + GPU + DB. Always returns 200 (ERR-1)."""
    accel = detect_accelerator()
    cache_entries = 0
    try:
        if settings.cache_dir.is_dir():
            cache_entries = sum(1 for _ in settings.cache_dir.iterdir())
    except OSError:
        logger.warning("cache_dir_iter_failed error_id=CACHE_DIR_RO", exc_info=True)

    db_reachable = await _ping_db(request.app.state.engine)

    return HealthResponse(
        status="ok",
        version="0.1.0",
        data_dir_writable=_probe_data_dir_writable(),
        cache_entries=cache_entries,
        db_reachable=db_reachable,
        **_accelerator_to_health(accel),
    )
