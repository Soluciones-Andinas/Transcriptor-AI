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
import gc
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SAQueueTimeoutError

from .config import settings
from .gpu import AcceleratorInfo, detect_accelerator
from .pipeline import diarize as _diarize
from .pipeline import stt as _stt
from .pipeline.cleanup import purge_expired

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

    # H-2: the orchestrator's GPU lock is module-level → per-process. Running
    # multiple workers behind a load balancer breaks AC-6 (a second worker
    # would NOT see the first worker's lock and could trigger concurrent
    # GPU jobs, OOM-ing). Detect common multi-worker env vars and refuse
    # to start so the operator catches the misconfiguration immediately.
    _enforce_single_worker_or_warn()

    # DATA_DIR/{models,cache,uploads} (RF-CACHE-01 step 2 + H-4 startup).
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "data_dirs_ready models_dir=%s cache_dir=%s uploads_dir=%s",
        settings.models_dir,
        settings.cache_dir,
        settings.uploads_dir,
    )

    # H-4: purge orphan uploads from a previous crashed run. A fresh
    # process has no in-flight requests by definition, so EVERY file in
    # uploads_dir is a leftover that the API endpoint's cleanup-finally
    # never reached (container kill -9, OOM, GPU panic, etc.). Best-effort
    # — failures are logged, never abort startup.
    _purge_orphan_uploads()

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

    # Capa 3 — load Whisper + pyannote models. Both loaders run in a worker
    # thread so the event loop stays responsive while CUDA churns. Loader
    # failures NEVER abort startup (D-028, AC-15): the service stays UP and
    # /health surfaces a per-model "error" + detail; POST /api/transcriptions
    # will respond 503 MODELS_NOT_LOADED (Batch 6) until the operator fixes
    # HF_TOKEN, the model cache, etc.
    app.state.whisper_status = "loading"
    app.state.whisper_detail = None
    app.state.whisper_model = None
    try:
        app.state.whisper_model = await asyncio.to_thread(
            _stt.load_whisper_model,
            settings.whisper_model,
            settings.whisper_device,
            settings.compute_type,
        )
        app.state.whisper_status = "ready"
        logger.info(
            "whisper_model_ready model=%s device=%s compute_type=%s",
            settings.whisper_model,
            settings.whisper_device,
            settings.compute_type,
        )
    except _stt.WhisperLoadError as exc:
        # H-5: typed detail discriminator surfaces in /health.whisper_detail
        # and POST 503 body verbatim. Operator distinguishes
        # cuda_unavailable / cuda_oom_at_load / model_not_found /
        # compute_type_unsupported / unknown without parsing free text.
        app.state.whisper_status = "error"
        app.state.whisper_detail = exc.detail
        logger.warning(
            "whisper_load_failed error_id=WHISPER_LOAD_ERROR detail=%s",
            exc.detail,
            exc_info=True,
        )
    except Exception as exc:  # noqa: BLE001
        # Defensive: should be unreachable now that load_whisper_model wraps
        # all upstream failures in WhisperLoadError. Kept so a bug in the
        # classifier doesn't crash startup.
        app.state.whisper_status = "error"
        app.state.whisper_detail = _stt.DETAIL_LOAD_UNKNOWN
        logger.warning(
            "whisper_load_failed_uncategorized error_id=WHISPER_LOAD_ERROR_UNKNOWN exc=%s",
            exc,
            exc_info=True,
        )

    app.state.pyannote_status = "loading"
    app.state.pyannote_detail = None
    app.state.pyannote_pipeline = None
    try:
        app.state.pyannote_pipeline = await asyncio.to_thread(
            _diarize.load_pyannote_pipeline,
            settings.hf_token.get_secret_value(),
            settings.pyannote_model,
        )
        app.state.pyannote_status = "ready"
        logger.info("pyannote_pipeline_ready model=%s", settings.pyannote_model)
    except _diarize.PyannoteLoadError as exc:
        app.state.pyannote_status = "error"
        app.state.pyannote_detail = exc.detail
        logger.warning(
            "pyannote_load_failed error_id=PYANNOTE_LOAD_ERROR detail=%s",
            exc.detail,
        )
    except Exception as exc:  # noqa: BLE001
        app.state.pyannote_status = "error"
        app.state.pyannote_detail = _diarize.DETAIL_UNKNOWN
        logger.error(
            "pyannote_load_failed error_id=PYANNOTE_LOAD_ERROR_UNKNOWN exc=%s",
            exc,
            exc_info=True,
        )

    # Pipeline lock block — defaults until Batch 5 wires the asyncio.Lock.
    # Surfacing them now keeps /health's response shape stable across batches.
    app.state.pipeline_lock_held = False
    app.state.pipeline_active_job_id = None
    app.state.pipeline_queue_depth = 0

    # Capa 3 Batch 7 — cleanup loop. Spawns a background task that calls
    # ``purge_expired`` every ``cache_cleanup_interval_seconds``. Cancelled
    # in the finally block on shutdown so the loop does not leak across
    # ASGI restarts (and tests via LifespanManager get a clean tear-down).
    app.state.cleanup_task = asyncio.create_task(_cleanup_loop())
    logger.info(
        "cleanup_task_spawned interval_seconds=%s",
        settings.cache_cleanup_interval_seconds,
    )

    try:
        yield
    finally:
        # Cancel the cleanup loop FIRST so it doesn't observe a half-disposed
        # engine if the dispose step took non-trivial time.
        cleanup_task = getattr(app.state, "cleanup_task", None)
        if cleanup_task is not None and not cleanup_task.done():
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("cleanup_task_cancelled")

        # ERR-1: dispose must not propagate; if the DB was already down,
        # closing dead connections itself raises. Log + swallow.
        try:
            await _db.engine.dispose()
            logger.info("db_engine_disposed")
        except Exception:
            logger.error("db_engine_dispose_failed error_id=DB_DISPOSE_FAILED", exc_info=True)

        # CR-4: explicit model cleanup. delattr alone releases the Python
        # reference but tensors hold CUDA-native memory that the torch
        # allocator caches by default; without explicit empty_cache + GC,
        # VRAM is not returned to the driver between LifespanManager
        # cycles in tests, and a long-running container that re-loads
        # models (future feature) would slowly leak.
        _release_models(app)

        # Defensive cleanup so back-to-back LifespanManager(app) entries in
        # the same test run don't inherit prior state (mirrors D-015 lesson).
        for attr in (
            "whisper_status",
            "whisper_detail",
            "whisper_model",
            "pyannote_status",
            "pyannote_detail",
            "pyannote_pipeline",
            "pipeline_lock_held",
            "pipeline_active_job_id",
            "pipeline_queue_depth",
            "cleanup_task",
        ):
            if hasattr(app.state, attr):
                delattr(app.state, attr)
        logger.info("service_stopping")


async def _cleanup_loop() -> None:
    """Background loop that periodically purges expired cache entries.

    Runs ``purge_expired`` in a worker thread (sync IO) so the event loop
    stays responsive. Cancellation is propagated cleanly: the inner
    ``await asyncio.sleep`` is the cancellation point and the
    ``CancelledError`` is allowed to escape so the lifespan teardown can
    await + handle it.

    A failure inside ``purge_expired`` itself is caught and logged so a
    transient filesystem error (e.g. a permissions hiccup) does not kill
    the loop and leave the cache to grow unbounded.
    """
    while True:
        try:
            await asyncio.sleep(settings.cache_cleanup_interval_seconds)
        except asyncio.CancelledError:
            raise

        try:
            await asyncio.to_thread(
                purge_expired,
                settings.cache_dir,
                settings.cache_ttl_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception(
                "cache_cleanup_iteration_failed error_id=CACHE_CLEANUP_LOOP_ERROR"
            )


def _enforce_single_worker_or_warn() -> None:
    """Warn (loudly) when env signals multiple uvicorn/gunicorn workers (H-2).

    The orchestrator's ``_orchestrator_lock`` is module-level and therefore
    process-local. With WEB_CONCURRENCY > 1 (or similar), a second worker
    has its own copy of the lock and would happily start a second GPU job
    while another worker holds the GPU — guaranteed to OOM the 8 GB rig.

    Detected variables (in priority order):
    - ``WEB_CONCURRENCY``  — gunicorn/uvicorn workers count
    - ``UVICORN_WORKERS``  — uvicorn-specific
    - ``GUNICORN_WORKERS`` — gunicorn-specific

    On detection of >1, log an ERROR with a clear remediation note. We log
    instead of hard-aborting because some local-dev workflows benignly set
    these to 2 for unrelated reasons; the operator should see the warning
    and act. AC-6 is about per-process semantics; documenting the
    assumption here is the spec-aligned response.
    """
    candidates = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS")
    for var in candidates:
        raw = os.environ.get(var)
        if raw is None:
            continue
        try:
            workers = int(raw)
        except ValueError:
            continue
        if workers > 1:
            logger.error(
                "multi_worker_detected error_id=MULTI_WORKER_GPU_LOCK_BROKEN "
                "var=%s workers=%d remediation=%s",
                var,
                workers,
                "set to 1; the GPU lock is process-local (ADR-005, AC-6)",
            )
            return
    logger.info("single_worker_assumption_validated")


def _purge_orphan_uploads() -> None:
    """Delete every file in ``settings.uploads_dir`` (H-4 startup).

    A fresh process has no in-flight uploads by definition, so any file
    present in uploads_dir is a leftover from a previous crashed run
    (container kill -9, OOM, GPU panic, ASGI shutdown that beat the
    endpoint's cleanup-finally). Best-effort: failures are logged but
    never abort startup. The TTL-based cleanup_loop only walks
    ``cache_dir``; uploads need this one-shot startup pass.
    """
    uploads_dir = settings.uploads_dir
    if not uploads_dir.is_dir():
        return
    purged = 0
    for entry in uploads_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            entry.unlink()
            purged += 1
        except OSError:
            logger.warning(
                "upload_orphan_unlink_failed path=%s error_id=UPLOAD_ORPHAN_LEAK",
                entry,
                exc_info=True,
            )
    if purged:
        logger.info("upload_orphans_purged count=%d", purged)


def _release_models(app: FastAPI) -> None:
    """Drop CUDA-bound model references and force VRAM release (CR-4).

    ``delattr(app.state, "whisper_model")`` would release the Python
    reference but the torch allocator caches freed blocks for reuse; on
    a single-process container that exits anyway the OS reclaims, but
    inside test runs LifespanManager spins app up + down many times and
    the cache accumulates. ``torch.cuda.empty_cache()`` returns those
    cached blocks to the driver. ``gc.collect()`` forces immediate
    finalization of tensor wrappers so the empty_cache call sees them.

    Best-effort: torch may not be installed (Capa 1+2 deployments) and
    CUDA may not be available — both cases swallow ImportError /
    AttributeError and continue.
    """
    # Drop strong references first so GC can reclaim the tensors.
    for attr in ("whisper_model", "pyannote_pipeline"):
        if getattr(app.state, attr, None) is not None:
            try:
                setattr(app.state, attr, None)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "model_attr_clear_failed attr=%s error_id=MODEL_RELEASE_FAILED",
                    attr,
                    exc_info=True,
                )

    # Force GC so any lingering tensor wrappers run their __del__ and
    # release the underlying CUDA memory back to torch's allocator.
    gc.collect()

    # Return cached CUDA blocks to the driver. Optional dependency: if
    # torch is not installed (Capa 1+2 image) or CUDA is unavailable,
    # this is a no-op.
    try:
        import torch  # type: ignore[import-not-found]

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            logger.info("cuda_empty_cache_done")
    except (ImportError, AttributeError):
        # No torch, or torch.cuda missing on a CPU-only build — no-op.
        pass


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


# Capa 3 — wire the transcriptions API (POST + GET /api/transcriptions).
# The router uses Depends(get_current_user_mcp) for bearer auth and
# Depends(get_session) for the request-scoped DB session.
from .api import transcriptions_router  # noqa: E402

app.include_router(transcriptions_router)


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
class ModelsHealth(BaseModel):
    """Per-model load state surfaced by /health (SPEC-capa3 AC-9 + AC-15).

    `whisper_detail` and `pyannote_detail` are populated only when the
    corresponding status is ``"error"``. The detail strings for pyannote
    are stable discriminators (``hf_token_missing`` etc., see
    ``pipeline.diarize.DETAIL_*``); the detail string for whisper is the
    raw runtime error message — there is no enumerated catalogue yet
    because the failure modes are heterogeneous (CUDA driver, OOM at
    load, FS missing model cache, ...).
    """

    whisper: str  # "ready" | "loading" | "error"
    pyannote: str  # "ready" | "loading" | "error"
    whisper_detail: str | None = None
    pyannote_detail: str | None = None
    vram_used_mb: int | None = None


class PipelineHealth(BaseModel):
    """Pipeline lock block — defaults until Batch 5 wires the asyncio lock."""

    lock_held: bool = False
    active_job_id: str | None = None
    queue_depth: int = 0


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
    models: ModelsHealth
    pipeline: PipelineHealth


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

    state = request.app.state
    vram_used_mb: int | None = None
    if accel.vram_total_mb is not None and accel.vram_free_mb is not None:
        vram_used_mb = max(accel.vram_total_mb - accel.vram_free_mb, 0)
    models_health = ModelsHealth(
        whisper=getattr(state, "whisper_status", "loading"),
        pyannote=getattr(state, "pyannote_status", "loading"),
        whisper_detail=getattr(state, "whisper_detail", None),
        pyannote_detail=getattr(state, "pyannote_detail", None),
        vram_used_mb=vram_used_mb,
    )
    pipeline_health = PipelineHealth(
        lock_held=getattr(state, "pipeline_lock_held", False),
        active_job_id=getattr(state, "pipeline_active_job_id", None),
        queue_depth=getattr(state, "pipeline_queue_depth", 0),
    )

    return HealthResponse(
        status="ok",
        version="0.1.0",
        data_dir_writable=_probe_data_dir_writable(),
        cache_entries=cache_entries,
        db_reachable=db_reachable,
        models=models_health,
        pipeline=pipeline_health,
        **_accelerator_to_health(accel),
    )
