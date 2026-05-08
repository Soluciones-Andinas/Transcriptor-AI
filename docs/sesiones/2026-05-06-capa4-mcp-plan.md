# PLAN-capa4-mcp-v1

> **Capa 4 — MCP Server (Streamable HTTP) + chunked upload pattern**
>
> **Spec referente**: [`SPEC-capa4-mcp-v1`](./2026-05-06-capa4-mcp-spec.md) (16 ACs, 12 errores tipados, 8 ALTs).
> **Branch**: `feat/capa4-mcp` (ya cortada de master post-merge Capa 3).
> **Convención TDD**: RED → GREEN → COMMIT por task. Un commit RED + un commit GREEN + commit refactor opcional. Hooks `pre-commit` corren ruff.
> **Convenciones de commit**:
> - `test(<scope>): SPEC-capa4 <AC-id> — <desc>` para RED.
> - `feat(<scope>): SPEC-capa4 <AC-id> — <desc>` para GREEN.
> - `chore(<scope>): SPEC-capa4 — <desc>` para tareas no-test (migration, dep bump, deprecation flag).
> - Sin `Co-Authored-By` (atribución global desactivada).

---

## Test mapping (AC → tasks)

| AC | Tests planeados | Batch.Task |
|---|---|---|
| AC-1 | E2E `request_upload_url → POST /api/upload → start_transcription` | B2.1 + B2.3 + B3.1 + B6.2 |
| AC-2 | Cross-user cache miss (per-user) | B3.1 (assert) + B6.2 |
| AC-3 | `list_my_transcriptions` cross-user isolation | B4.1 |
| AC-4 | `search_my_transcriptions` FTS rank + snippet | B4.2 |
| AC-5 | `get_transcription` cross-user 404 | B4.3 |
| AC-6 | Resource `transcription://<id>` | B5.1 |
| AC-7 | Resource `transcription://<id>/images/<image_id>` + image upload | B2.4 + B5.1 |
| AC-8 | Bearer revoked / inexistente / sin header | B1.2 |
| AC-9 | Lock GPU contention en `start_transcription` | B3.1 (mock orchestrate con sleep) |
| AC-10 | `expires_at` de upload session ya pasado → 404 | B3.2 |
| AC-11 | `delete_transcription` soft delete + cascade images + idempotencia | B5.2 |
| AC-12 | MCP handshake + list-tools muestra 7 tools | B1.1 + B6.2 |
| AC-13 | `GET /auth/me.mcp_url` consistente con `${PUBLIC_BASE_URL}/mcp` | B1.1 (smoke) |
| AC-14 | `mcp_bearers.last_used_at` bumped post-call | B1.2 |
| AC-15 | Migration aplicada — columna `upload_bearer_hash` NOT NULL | B0.1 |
| AC-16 | Endpoint legacy `POST /api/transcriptions` `deprecated=true` + log WARN | B6.1 |

Tests externos (no listados): smoke E2E en rig (Franco lo corre, B6.2 documenta el procedure).

---

## Convenciones del plan

- **Working dir**: `/Users/francobertoldi/Documents/Sandinas/IA-Tasks/IA-Tasks-Investigación-Estrategia/transcription-api`. Paths absolutos.
- **Venv local**: `.venv/bin/python -m pytest ...`. Ya tiene Capa 1+2+3 deps. Los `[pipeline]` extras (torch, whisperx, pyannote) NO están en el venv local — los tests deben mockear.
- **Tablas tocadas**: solo `upload_sessions` (Batch 0). Nada más se modifica del schema.
- **Áreas read-only**: `wiki/**` (drifts van a `docs/sesiones/2026-05-05-wiki-drifts.md`), `auth/**` (excepto factorización menor de `verify_bearer`), `pipeline/**` (excepto reuso transparente de `orchestrate`), `db/**` salvo `models/upload_session.py`.
- **Imports lazy**: si una task agrega un módulo que importa `mcp.server.fastmcp`, asegurar que `import transcription_api.<modulo>` no rompe en CPU dev box. El SDK `mcp` es ligero y puede importarse al top sin problemas; los tests deben tener fixture que provea un `FastMCP` minimal o usar el real con tools mockeadas.

---

## Batches

### Batch 0 — Schema migration `add_upload_bearer_hash` (D-044-impl)

**Goal**: agregar columna `upload_bearer_hash TEXT NOT NULL` a `upload_sessions`. Pre-flight check defensivo.

#### Task 0.1 — RED test: column exists post `alembic upgrade head`

**File**: `tests/integration/test_alembic.py` (extender, ya existe).

```python
@pytest.mark.requires_docker
async def test_upload_sessions_has_upload_bearer_hash_after_upgrade(pg_engine):
    """AC-15: post upgrade head, upload_sessions.upload_bearer_hash existe NOT NULL."""
    async with pg_engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT column_name, is_nullable, data_type "
            "FROM information_schema.columns "
            "WHERE table_name='upload_sessions' AND column_name='upload_bearer_hash'"
        ))
        row = result.first()
        assert row is not None, "Column upload_bearer_hash missing"
        assert row.is_nullable == "NO", "Column must be NOT NULL"
        assert row.data_type in ("text", "character varying")
```

**Run** `.venv/bin/python -m pytest tests/integration/test_alembic.py -k upload_bearer_hash -v` → FAIL (column missing).

**Commit**: `test(db): SPEC-capa4 AC-15 — RED test for upload_bearer_hash column`.

#### Task 0.2 — GREEN: alembic migration + ORM

**Files**:
- `alembic/versions/<rev>_add_upload_bearer_hash.py` (nuevo, generado con `alembic revision -m add_upload_bearer_hash`).
- `src/transcription_api/db/models/upload_session.py` (modificar: agregar columna).

**Migration**:

```python
"""add upload_bearer_hash to upload_sessions

Revision ID: <rev>
Revises: 352c7acf6f15
Create Date: 2026-05-06 ...
"""
from alembic import op
import sqlalchemy as sa

revision = "<rev>"
down_revision = "352c7acf6f15"

def upgrade():
    # Pre-flight: la tabla está vacía en producción al deploy. Si encuentra rows
    # con NULL en este punto (debería ser imposible — no hay defaults), abortar.
    bind = op.get_bind()
    nulls = bind.execute(sa.text(
        "SELECT count(*) FROM upload_sessions"
    )).scalar()
    assert nulls == 0, (
        "upload_sessions tiene rows pre-existentes; abortar migration "
        "para que el operator decida cómo backfillear upload_bearer_hash. "
        f"rows={nulls}"
    )
    op.add_column(
        "upload_sessions",
        sa.Column("upload_bearer_hash", sa.Text(), nullable=False),
    )

def downgrade():
    op.drop_column("upload_sessions", "upload_bearer_hash")
```

**ORM update** (`db/models/upload_session.py`, agregar después de `nonce`):

```python
upload_bearer_hash: Mapped[str] = mapped_column(Text, nullable=False)
```

**Run** `.venv/bin/python -m pytest tests/integration/test_alembic.py -k upload_bearer_hash -v` → PASS.

**Commit**: `feat(db): SPEC-capa4 AC-15 — add upload_bearer_hash column + ORM`.

---

### Batch 1 — MCP foundation: server + middleware + DB session ctx mgr

**Goal**: app.mount("/mcp", ...) funciona, middleware valida bearer, sesión DB del tool maneja `db.info["user_id"]` correctamente.

#### Task 1.1 — RED: `from transcription_api.mcp import mcp_app` + `GET /mcp` no 404

**Files (RED tests)**:
- `tests/unit/mcp/test_mcp_module_imports.py` (nuevo).
- `tests/integration/mcp/test_mcp_mount.py` (nuevo).

```python
# tests/unit/mcp/test_mcp_module_imports.py
def test_mcp_module_imports():
    """AC-12: el módulo mcp es importable y expone mcp_app."""
    from transcription_api import mcp as _mcp
    assert hasattr(_mcp, "mcp_app"), "mcp_app must be exported"
    assert callable(_mcp.mcp_app) or hasattr(_mcp.mcp_app, "__call__"), (
        "mcp_app must be ASGI-callable"
    )
```

```python
# tests/integration/mcp/test_mcp_mount.py
import pytest
from httpx import ASGITransport, AsyncClient
from transcription_api.main import app

@pytest.mark.asyncio
async def test_mcp_endpoint_does_not_404():
    """AC-12: GET /mcp no devuelve 404 de FastAPI (el sub-app está mounted)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # POST con JSON-RPC handshake mínimo del SDK MCP.
        # Sin auth header → debería retornar 401 (manejo del middleware), NO 404.
        resp = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
    assert resp.status_code != 404, "MCP sub-app not mounted"
```

**Run** → FAIL (modulo no existe).

**Commit**: `test(mcp): SPEC-capa4 AC-12 — RED tests for MCP module + mount`.

#### Task 1.2 — GREEN: skeleton MCP module + mount

**Files**:
- `pyproject.toml` (agregar `mcp[server]>=1.5,<2.0` a `dependencies`).
- `src/transcription_api/mcp/__init__.py` (nuevo).
- `src/transcription_api/mcp/server.py` (nuevo).
- `src/transcription_api/main.py` (modificar: agregar mount post-routers).

```python
# src/transcription_api/mcp/server.py
"""FastMCP server factory — Capa 4 RF-MCP-00.

Single instance shared with the FastAPI app. Tools and resources are
registered via `register_tools(server)` / `register_resources(server)`
called in `__init__.py` at import time.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp_server = FastMCP(name="transcription-api")
```

```python
# src/transcription_api/mcp/__init__.py
"""Public API of the mcp module: mcp_app (ASGI sub-app for FastAPI mount)."""
from .server import mcp_server
# Tools/resources registered here as Batch 2+ tasks land:
# from .tools import register_tools
# from .resources import register_resources
# register_tools(mcp_server)
# register_resources(mcp_server)

mcp_app = mcp_server.streamable_http_app()
__all__ = ["mcp_server", "mcp_app"]
```

```python
# src/transcription_api/main.py — agregar al final post `include_router` calls
from .mcp import mcp_app  # noqa: E402

app.mount("/mcp", mcp_app)
```

**Run** → PASS.

**Commit**: `feat(mcp): SPEC-capa4 AC-12 — mount MCP streamable HTTP sub-app at /mcp`.

#### Task 1.3 — RED: middleware bearer extraction + 401 en bearer inválido/revocado/ausente (AC-8)

**Files**:
- `tests/integration/mcp/test_mcp_middleware.py` (nuevo).

Test plan: registrar una tool de prueba `_test_ping` que retorna `{"ok": True, "user_id": str(current_user_id)}`, hacer 4 calls:

```python
@pytest.mark.asyncio
async def test_mcp_no_bearer_returns_401(mcp_client):
    """AC-8: sin Authorization header → MCP_BEARER_INVALID."""
    resp = await mcp_client.call_tool("_test_ping", {})
    assert resp.is_error
    assert resp.error.data["error_code"] == "MCP_BEARER_INVALID"

@pytest.mark.asyncio
async def test_mcp_invalid_bearer_returns_401(mcp_client_with_bearer):
    """AC-8: bearer inexistente."""
    resp = await mcp_client_with_bearer("totally-not-a-real-bearer").call_tool("_test_ping", {})
    assert resp.error.data["error_code"] == "MCP_BEARER_INVALID"

@pytest.mark.asyncio
async def test_mcp_revoked_bearer_returns_401(mcp_client_with_bearer, revoked_bearer_plaintext):
    """AC-8: bearer revoked."""
    resp = await mcp_client_with_bearer(revoked_bearer_plaintext).call_tool("_test_ping", {})
    assert resp.error.data["error_code"] == "MCP_BEARER_REVOKED"

@pytest.mark.asyncio
async def test_mcp_valid_bearer_passes(mcp_client_with_bearer, active_bearer_plaintext, user_id):
    """AC-8 + AC-14: bearer válido → handler ejecuta + last_used_at bumped."""
    resp = await mcp_client_with_bearer(active_bearer_plaintext).call_tool("_test_ping", {})
    assert resp.content[0].data["user_id"] == str(user_id)
    # AC-14: re-fetch bearer row, last_used_at should be recent
    # (assertion in fixture or follow-up DB query)
```

Fixtures necesarias en `tests/integration/mcp/conftest.py`:
- `mcp_client`: client del SDK MCP apuntando al app via ASGI transport.
- `mcp_client_with_bearer(plaintext)`: factory que arma headers Authorization.
- `active_bearer_plaintext`, `revoked_bearer_plaintext`, `user_id`: fixtures DB seteando rows.

**Commit**: `test(mcp): SPEC-capa4 AC-8 + AC-14 — RED tests for bearer middleware`.

#### Task 1.4 — GREEN: middleware + DB session ctx mgr + `_test_ping` tool

**Files**:
- `src/transcription_api/mcp/middleware.py` (nuevo).
- `src/transcription_api/mcp/session.py` (nuevo).
- `src/transcription_api/mcp/tools/__init__.py` (nuevo, registra tools).
- `src/transcription_api/mcp/tools/_test_ping.py` (nuevo, helper de tests).

```python
# src/transcription_api/mcp/session.py
"""DB session lifecycle for MCP tool/resource handlers.

FastMCP no usa FastAPI Depends. Cada handler abre y cierra su propia
AsyncSession via este context manager, que arma `db.info["user_id"]`
para que el listener fail-closed (ADR-015) inyecte WHERE user_id=X
automáticamente. Commit en path feliz, rollback en exception.
"""
from __future__ import annotations
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import async_session_factory


@asynccontextmanager
async def mcp_request_session(user_id: UUID):
    session: AsyncSession = async_session_factory()
    session.info["user_id"] = user_id
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
```

```python
# src/transcription_api/mcp/middleware.py
"""Bearer auth middleware for MCP requests (RF-MCP-11).

Extracts `Authorization: Bearer <plaintext>` from the FastMCP request
context, validates against `mcp_bearers.token_hash` (reusing
`auth.mcp_bearer.verify_bearer`), and exposes `current_user_id` for
tool handlers via a context-local. Best-effort `last_used_at` bump.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from uuid import UUID

from sqlalchemy import update

from ..auth.mcp_bearer import verify_bearer, BearerInvalid, BearerRevoked
from ..db.scoping import bypass_scoping
from ..db.session import async_session_factory
from ..db.models import McpBearer

logger = logging.getLogger("transcription_api.mcp.middleware")

_current_user_id: ContextVar[UUID | None] = ContextVar("_current_user_id", default=None)


class McpAuthError(Exception):
    """Mapped by tool handlers to MCP_BEARER_INVALID / _REVOKED."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


async def authenticate_request(authorization_header: str | None) -> UUID:
    """Validate bearer + arm current_user_id ContextVar. Raise McpAuthError on fail."""
    if not authorization_header or not authorization_header.lower().startswith("bearer "):
        raise McpAuthError("MCP_BEARER_INVALID")
    plaintext = authorization_header[len("Bearer "):].strip()

    async with async_session_factory() as session:
        with bypass_scoping(session):
            try:
                bearer_row = await verify_bearer(session, plaintext)
            except BearerInvalid:
                raise McpAuthError("MCP_BEARER_INVALID")
            except BearerRevoked:
                raise McpAuthError("MCP_BEARER_REVOKED")

            # AC-14: best-effort last_used_at bump.
            try:
                await session.execute(
                    update(McpBearer)
                    .where(McpBearer.id == bearer_row.id)
                    .values(last_used_at=text("clock_timestamp()"))
                )
                await session.commit()
            except Exception:
                logger.warning(
                    "mcp_last_used_at_bump_failed bearer_id=%s", bearer_row.id,
                    exc_info=True,
                )

    _current_user_id.set(bearer_row.user_id)
    return bearer_row.user_id


def get_current_user_id() -> UUID:
    """Read the user_id armed by the middleware. Tools call this synchronously."""
    uid = _current_user_id.get()
    if uid is None:
        raise McpAuthError("MCP_BEARER_INVALID")
    return uid
```

**Wire en FastMCP**: el SDK 1.5+ soporta middleware via `@mcp_server.middleware` o equivalente. Si la API exacta del SDK no expone middleware, usar wrapper alrededor de cada `@mcp_server.tool` decorator: una helper `@authenticated_tool` que llama `authenticate_request(headers.get("authorization"))` antes del cuerpo. Decisión final del wire en el GREEN — leer la doc del SDK al implementar.

```python
# src/transcription_api/mcp/tools/_test_ping.py
"""Test-only tool that returns the authenticated user_id. Mounted only in tests."""
from ..middleware import get_current_user_id
from ..server import mcp_server

@mcp_server.tool(name="_test_ping")
def test_ping() -> dict:
    return {"ok": True, "user_id": str(get_current_user_id())}
```

**Run** → PASS.

**Commit**: `feat(mcp): SPEC-capa4 AC-8 + AC-14 — bearer middleware + session ctx mgr`.

---

### Batch 2 — `request_upload_url` tool + `POST /api/upload` endpoint

**Goal**: el flow `MCP tool create → REST upload → ready for start_transcription` queda funcional para audio + imagen.

#### Task 2.1 — RED: `request_upload_url(kind=audio)` happy path

**File**: `tests/integration/mcp/test_request_upload_url.py` (nuevo).

```python
@pytest.mark.asyncio
async def test_request_upload_url_audio_happy(mcp_client_with_bearer, active_bearer_plaintext, user_id, db_session):
    """AC-1 partial: tool retorna upload_url + upload_id + bearer + expires_at; row INSERTeada con upload_bearer_hash."""
    client = mcp_client_with_bearer(active_bearer_plaintext)
    resp = await client.call_tool("request_upload_url", {
        "kind": "audio",
        "file_size_bytes": 10_000_000,
    })
    body = resp.content[0].data
    assert "upload_url" in body
    assert "upload_id" in body
    assert "bearer" in body
    assert "expires_at" in body
    # plaintext es URL-safe 32+ chars
    assert len(body["bearer"]) >= 32

    # DB row exists with hash != plaintext
    from hashlib import sha256
    expected_hash = sha256(body["bearer"].encode()).hexdigest()
    rows = (await db_session.execute(
        text("SELECT upload_bearer_hash FROM upload_sessions WHERE id=:id"),
        {"id": body["upload_id"]},
    )).all()
    assert len(rows) == 1
    assert rows[0].upload_bearer_hash == expected_hash
    # plaintext NOT stored
    assert body["bearer"] not in str(rows[0])
```

**Commit**: `test(mcp): SPEC-capa4 AC-1 — RED test request_upload_url audio path`.

#### Task 2.2 — GREEN: `request_upload_url` implementation

**Files**:
- `src/transcription_api/mcp/tools/upload.py` (nuevo).
- `src/transcription_api/mcp/tools/__init__.py` (registrar).

```python
# src/transcription_api/mcp/tools/upload.py
"""Tool request_upload_url — RF-MCP-01."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID, uuid4

from ..middleware import get_current_user_id
from ..server import mcp_server
from ..session import mcp_request_session
from ...config import settings
from ...db.models import UploadSession, Transcription
from sqlalchemy import select


_AUDIO_MIME_DEFAULT = "audio/mpeg"
_IMAGE_MIMES_OK = {"image/png", "image/jpeg", "image/webp", "image/gif"}


@mcp_server.tool(name="request_upload_url")
async def request_upload_url(
    kind: str,
    file_size_bytes: int,
    mime_type: str | None = None,
    transcription_id: str | None = None,
) -> dict:
    if kind not in ("audio", "image"):
        return _error("INVALID_PARAMETER", "kind must be 'audio' or 'image'", 400)
    if file_size_bytes <= 0:
        return _error("INVALID_PARAMETER", "file_size_bytes must be > 0", 400)

    if kind == "audio":
        max_bytes = settings.max_upload_mb * 1024 * 1024
    else:
        max_bytes = settings.max_image_upload_mb * 1024 * 1024
        if mime_type not in _IMAGE_MIMES_OK:
            return _error("INVALID_PARAMETER", f"mime_type {mime_type!r} not in {_IMAGE_MIMES_OK}", 400)

    if file_size_bytes > max_bytes:
        return _error("FILE_TOO_LARGE", f"exceeds {max_bytes // (1024*1024)} MB", 413,
                      extra={"max_mb": max_bytes // (1024*1024)})

    user_id = get_current_user_id()

    async with mcp_request_session(user_id) as db:
        if kind == "image":
            tid_uuid = UUID(transcription_id) if transcription_id else None
            if tid_uuid is None:
                return _error("INVALID_PARAMETER", "transcription_id required for kind=image", 400)
            owner_check = (await db.execute(
                select(Transcription.id).where(Transcription.id == tid_uuid)
            )).scalar_one_or_none()
            if owner_check is None:
                return _error("TRANSCRIPTION_NOT_FOUND", "transcription not found", 404)
        else:
            tid_uuid = None

        upload_id = uuid4()
        nonce = secrets.token_urlsafe(32)
        bearer_for_upload = secrets.token_urlsafe(32)
        upload_bearer_hash = sha256(bearer_for_upload.encode()).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.upload_session_ttl_seconds)

        # bearer_id = la fila del bearer principal del user — necesitamos lookup via context
        # Opción simple: lookup del bearer activo del user (asume único activo).
        bearer_id = await _resolve_active_bearer_id(db, user_id)

        row = UploadSession(
            id=upload_id,
            user_id=user_id,
            bearer_id=bearer_id,
            nonce=nonce,
            upload_bearer_hash=upload_bearer_hash,
            kind=kind,
            transcription_id=tid_uuid,
            expected_size_bytes=file_size_bytes,
            expected_mime_type=mime_type,
            expires_at=expires_at,
        )
        db.add(row)
        await db.flush()

        path = "/api/upload" if kind == "audio" else "/api/upload-image"
        upload_url = f"{settings.public_base_url}{path}?session={nonce}"

        return {
            "upload_url": upload_url,
            "upload_id": str(upload_id),
            "bearer": bearer_for_upload,
            "expires_at": expires_at.isoformat(),
        }


def _error(code: str, reason: str, http_status: int, extra: dict | None = None) -> dict:
    body = {"error_code": code, "reason": reason}
    if extra:
        body.update(extra)
    # Raise un MCP-typed error que el SDK mapea a JSON-RPC error.data
    raise ToolError(body, http_status=http_status)
```

(`ToolError` es un helper común a definir en `mcp/errors.py`; el SDK MCP traduce a JSON-RPC error con `data` field.)

**Resolver del bearer_id**: helper que hace `select(McpBearer.id).where(McpBearer.user_id == user_id, McpBearer.revoked_at.is_(None))` con `bypass_scoping` (porque el listener filtra por `user_id` ya, y queremos que el lookup del SELF-bearer funcione — actualmente el middleware ya hizo el lookup, podríamos pasarlo via ContextVar).

**Optimización defer**: por ahora exponer `_current_bearer_id: ContextVar[UUID]` en `middleware.py` y armarlo igual que `_current_user_id`.

**Commit**: `feat(mcp): SPEC-capa4 AC-1 — request_upload_url tool (audio + image branches)`.

#### Task 2.3 — RED: `request_upload_url(kind=image, transcription_id=ajeno)` → 404

```python
@pytest.mark.asyncio
async def test_request_upload_url_image_other_users_transcription_404(...):
    """AC-1: image upload referencing another user's transcription → 404."""
    # crear transcription del user B
    # request_upload_url con bearer del user A apuntando a esa transcription
    # esperar TRANSCRIPTION_NOT_FOUND
```

**Commit**: `test(mcp): SPEC-capa4 AC-1 — RED test image upload cross-user blocked`.

#### Task 2.4 — RED + GREEN: `POST /api/upload` (audio happy + bearer wrong + size mismatch)

**Files**:
- `tests/integration/api/test_upload.py` (nuevo).
- `src/transcription_api/api/upload.py` (nuevo, GREEN).
- `src/transcription_api/api/__init__.py` (modificar: exportar `upload_router`).
- `src/transcription_api/main.py` (incluir el router).

Tests:
```python
@pytest.mark.asyncio
async def test_post_upload_audio_happy(client, audio_upload_session, audio_bearer_plaintext, tmp_uploads):
    """AC-1: POST /api/upload con bearer correcto + nonce → 200, escribe a disk, status='uploaded'."""

@pytest.mark.asyncio
async def test_post_upload_wrong_bearer_returns_401(...):
    """AC-1: bearer wrong → MCP_BEARER_INVALID."""

@pytest.mark.asyncio
async def test_post_upload_size_too_large_returns_413(...):
    """AC-1: file size > expected_size_bytes * 1.05 → FILE_TOO_LARGE."""

@pytest.mark.asyncio
async def test_post_upload_session_expired_returns_404(...):
    """AC-10: nonce válido pero expires_at pasado → UPLOAD_SESSION_NOT_FOUND."""
```

GREEN:

```python
# src/transcription_api/api/upload.py
"""POST /api/upload — RF-MCP-03 + RF-IMG.

Recibe multipart `file` con query `?session=<nonce>` y header
`Authorization: Bearer <plaintext_ephemeral>`. Valida hash contra
`upload_sessions.upload_bearer_hash` con hmac.compare_digest.
"""
from __future__ import annotations
import hmac
import logging
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..db.models import UploadSession, Image
from ..db.scoping import bypass_scoping
from fastapi import Depends

logger = logging.getLogger("transcription_api.api.upload")
router = APIRouter(prefix="/api", tags=["uploads"])


@router.post("/upload")
async def upload_audio(
    request: Request,
    session_nonce: str = Query(..., alias="session"),
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    return await _upload_common(request, session_nonce, file, authorization, db, expected_kind="audio")


@router.post("/upload-image")
async def upload_image(
    request: Request,
    session_nonce: str = Query(..., alias="session"),
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    return await _upload_common(request, session_nonce, file, authorization, db, expected_kind="image")


async def _upload_common(request, session_nonce, file, authorization, db, *, expected_kind):
    if not authorization or not authorization.lower().startswith("bearer "):
        return _error_resp(401, "MCP_BEARER_INVALID", "missing bearer")
    plaintext = authorization[len("Bearer "):].strip()
    received_hash = sha256(plaintext.encode()).hexdigest()

    # Lookup upload session (admin context — no user_id armed yet).
    with bypass_scoping(db):
        row = (await db.execute(
            select(UploadSession).where(
                UploadSession.nonce == session_nonce,
                UploadSession.status == "requested",
            )
        )).scalar_one_or_none()

    if row is None:
        return _error_resp(404, "UPLOAD_SESSION_NOT_FOUND", "session not found or already consumed")
    if row.expires_at < datetime.now(timezone.utc):
        return _error_resp(404, "UPLOAD_SESSION_NOT_FOUND", "session expired")
    if row.kind != expected_kind:
        return _error_resp(400, "INVALID_PARAMETER", f"endpoint expects kind={expected_kind}, session has kind={row.kind}")
    if not hmac.compare_digest(received_hash, row.upload_bearer_hash):
        return _error_resp(401, "MCP_BEARER_INVALID", "bearer hash mismatch")

    # Stream file to disk while counting bytes (cap at expected*1.05).
    max_bytes = int(row.expected_size_bytes * 1.05)
    if expected_kind == "audio":
        target_dir = settings.uploads_dir / str(row.id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "original.bin"
    else:
        # imagen: temp path; move to final blob path post-validation
        target = settings.uploads_dir / f"_pending_image_{row.id}.bin"

    bytes_written = 0
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    fh.close()
                    target.unlink(missing_ok=True)
                    return _error_resp(413, "FILE_TOO_LARGE", "file exceeds expected size + 5% margin")
                fh.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise

    if expected_kind == "image":
        # Magic bytes verification + INSERT image + move to blobs/
        # ... (omitido por brevedad; usa python-magic o stdlib `imghdr`)
        pass

    # UPDATE upload_sessions → status='uploaded'
    with bypass_scoping(db):
        await db.execute(
            UploadSession.__table__.update()
            .where(UploadSession.id == row.id)
            .values(status="uploaded", uploaded_at=datetime.now(timezone.utc))
        )
        await db.commit()

    body = {"ok": True}
    if expected_kind == "audio":
        body["upload_id"] = str(row.id)
    else:
        body["image_id"] = str(image_id)
    logger.info("upload_received user_id=%s upload_id=%s kind=%s size=%d", row.user_id, row.id, expected_kind, bytes_written)
    return JSONResponse(status_code=200, content=body)


def _error_resp(status, code, reason):
    return JSONResponse(status_code=status, content={"detail": {"error_code": code, "reason": reason}})
```

**Commits**:
- `test(api): SPEC-capa4 AC-1 + AC-10 — RED tests POST /api/upload`.
- `feat(api): SPEC-capa4 AC-1 + AC-10 — POST /api/upload (audio + image branches)`.

---

### Batch 3 — `start_transcription` tool

**Goal**: tool MCP que invoca `pipeline.orchestrator.orchestrate(...)` con todos los kwargs correctos, maneja UPDATE upload_sessions, cleanup uploads dir.

#### Task 3.1 — RED: `start_transcription` happy path con `orchestrate` mockeado

**File**: `tests/integration/mcp/test_start_transcription.py`.

```python
@pytest.mark.asyncio
async def test_start_transcription_happy(mcp_client_with_bearer, uploaded_session, monkeypatch):
    """AC-1: tool retorna transcription_id; status='consumed'; uploads/<id>/ borrado."""
    fake_result = {
        "transcription_id": uuid4(),
        "audio_hash": "deadbeef" * 8,
        "language": "es",
        "duration_seconds": 30.5,
        "num_speakers": 2,
        "text_content": "SPEAKER_00: hola\nSPEAKER_01: chau",
        "segments": [],
        "metadata": {"cache_hit": False},
    }
    mock_orchestrate = AsyncMock(return_value=fake_result)
    monkeypatch.setattr("transcription_api.mcp.tools.transcription.orchestrate", mock_orchestrate)

    resp = await mcp_client_with_bearer(active_bearer).call_tool("start_transcription", {
        "upload_id": str(uploaded_session.id),
        "language": "es",
    })
    body = resp.content[0].data
    assert "transcription_id" in body
    assert body["status"] == "completed"
    assert body["cache_hit"] is False
    # status='consumed' + uploads dir cleaned
    # ... DB and FS asserts
```

#### Task 3.2 — GREEN: `start_transcription` implementation

```python
# src/transcription_api/mcp/tools/transcription.py
"""Tools: start_transcription, get_transcription, list_my_, search_my_, delete_."""
from __future__ import annotations
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from ..middleware import get_current_user_id
from ..server import mcp_server
from ..session import mcp_request_session
from ..errors import ToolError
from ...config import settings
from ...db.models import UploadSession, Transcription, Image
from ...pipeline.cache import CacheStore
from ...pipeline.orchestrator import (
    orchestrate, GPUBusy, PipelineTimeout,
)
from fastapi import Request  # not used directly; lifespan models accessed via app.state via SDK?


@mcp_server.tool(name="start_transcription")
async def start_transcription(
    upload_id: str,
    language: str = "es",
    min_speakers: int | None = 1,
    max_speakers: int | None = 8,
) -> dict:
    user_id = get_current_user_id()
    try:
        uid = UUID(upload_id)
    except ValueError:
        raise ToolError({"error_code": "INVALID_PARAMETER", "reason": "upload_id not a UUID"}, 400)

    async with mcp_request_session(user_id) as db:
        row = (await db.execute(
            select(UploadSession).where(UploadSession.id == uid, UploadSession.kind == "audio")
        )).scalar_one_or_none()
        if row is None:
            raise ToolError({"error_code": "UPLOAD_SESSION_NOT_FOUND", "reason": "upload not found"}, 404)
        if row.status == "consumed":
            raise ToolError({"error_code": "UPLOAD_SESSION_ALREADY_CONSUMED", "reason": "already consumed"}, 409)
        if row.status != "uploaded" or row.expires_at < datetime.now(timezone.utc):
            raise ToolError({"error_code": "UPLOAD_SESSION_NOT_FOUND", "reason": "not uploaded or expired"}, 404)

        upload_dir_for_session = settings.uploads_dir / str(uid)
        original = upload_dir_for_session / "original.bin"

        # Acceso al app.state — el SDK MCP debería exponer el ASGI scope.
        # Si no, los modelos vienen via singleton del módulo pipeline (ya cargados en lifespan).
        from ..mounts import get_app_state
        state = get_app_state()
        whisper_model = state.whisper_model
        pyannote_pipeline = state.pyannote_pipeline
        if state.whisper_status != "ready" or state.pyannote_status != "ready":
            raise ToolError({"error_code": "MODELS_NOT_LOADED", "reason": "models not ready"}, 503)

        cache_store = CacheStore(base_dir=settings.cache_dir)

        try:
            result = await orchestrate(
                user_id=user_id,
                db=db,
                file_path=original,
                original_filename=row.expected_mime_type or "audio.bin",  # mejor: agregar columna original_filename a upload_sessions
                original_size_bytes=row.expected_size_bytes,
                whisper_model=whisper_model,
                pyannote_pipeline=pyannote_pipeline,
                cache_store=cache_store,
                upload_dir=upload_dir_for_session,
                language=language,
                min_speakers=min_speakers,
                max_speakers=max_speakers,
            )
        except GPUBusy as exc:
            raise ToolError({"error_code": "LOCK_BUSY", "retry_after": exc.retry_after}, 503)
        except PipelineTimeout as exc:
            raise ToolError({"error_code": "PIPELINE_TIMEOUT", "timeout_seconds": exc.timeout_seconds}, 504)

        # UPDATE upload_session
        await db.execute(
            UploadSession.__table__.update()
            .where(UploadSession.id == uid)
            .values(status="consumed", consumed_at=datetime.now(timezone.utc))
        )

        # Cleanup upload dir
        try:
            if upload_dir_for_session.exists():
                shutil.rmtree(upload_dir_for_session)
        except OSError:
            logger.warning("upload_dir_cleanup_failed path=%s", upload_dir_for_session, exc_info=True)

        return {
            "transcription_id": str(result["transcription_id"]),
            "status": "completed",
            "cache_hit": result.get("metadata", {}).get("cache_hit", False),
        }
```

**Note**: el helper `get_app_state()` en `mcp/mounts.py` devuelve el `app.state` para acceder a los modelos cargados. El SDK MCP expone el ASGI scope; si no, la solución más simple es importar `app` desde `main.py` (con cuidado de no crear ciclo de imports — usar lazy `from ..main import app`).

**Commits**:
- `test(mcp): SPEC-capa4 AC-1 + AC-9 — RED tests start_transcription`.
- `feat(mcp): SPEC-capa4 AC-1 + AC-9 — start_transcription wraps orchestrate`.

#### Task 3.3 — RED + GREEN: edge cases (upload ajeno, already consumed, expired)

Tests para AC-10 + AC-2 (cross-user). Implementación cae naturalmente del listener fail-closed.

**Commits**: `test(mcp): SPEC-capa4 AC-2 + AC-10 — edge cases start_transcription`, `feat(mcp): SPEC-capa4 AC-2 + AC-10 — edge cases handled`.

---

### Batch 4 — `list_my_transcriptions` + `search_my_transcriptions` + `get_transcription`

#### Task 4.1 — RED + GREEN: `list_my_transcriptions` con paginación + cross-user (AC-3)

```python
# tests
async def test_list_returns_only_my_transcriptions(...):
    """AC-3: user A ve solo sus 5, no las 3 de user B."""
async def test_list_pagination(...):
    """offset+limit clip correctamente."""

# impl: src/transcription_api/mcp/tools/transcription.py
@mcp_server.tool(name="list_my_transcriptions")
async def list_my_transcriptions(limit: int = 20, offset: int = 0, sort: str = "created_at_desc") -> dict:
    if limit > 100: limit = 100
    user_id = get_current_user_id()
    sort_col = {
        "created_at_desc": Transcription.created_at.desc(),
        "created_at_asc": Transcription.created_at.asc(),
        "duration_desc": Transcription.duration_seconds.desc(),
    }.get(sort)
    if sort_col is None:
        raise ToolError({"error_code": "INVALID_PARAMETER", "reason": f"sort {sort!r} invalid"}, 400)

    async with mcp_request_session(user_id) as db:
        # listener filtra WHERE user_id=X automáticamente
        items = (await db.execute(
            select(Transcription)
            .where(Transcription.deleted_at.is_(None))
            .order_by(sort_col)
            .limit(limit).offset(offset)
        )).scalars().all()
        total = (await db.execute(
            select(func.count(Transcription.id)).where(Transcription.deleted_at.is_(None))
        )).scalar()
        return {
            "items": [_serialize_summary(row) for row in items],
            "total": total, "limit": limit, "offset": offset,
        }
```

**Commits**: `test(mcp): SPEC-capa4 AC-3 — RED list cross-user + pagination`, `feat(mcp): SPEC-capa4 AC-3 — list_my_transcriptions`.

#### Task 4.2 — RED + GREEN: `search_my_transcriptions` con FTS

```python
async def test_search_returns_matching_with_rank_and_snippet(...):
    """AC-4: query 'arquitectura' → match con rank>0, snippet con la palabra."""

# impl
@mcp_server.tool(name="search_my_transcriptions")
async def search_my_transcriptions(query: str, limit: int = 10) -> list[dict]:
    if not query.strip() or len(query) > 200:
        raise ToolError({"error_code": "INVALID_PARAMETER", "reason": "query empty or too long"}, 400)
    if limit > 50: limit = 50
    user_id = get_current_user_id()

    async with mcp_request_session(user_id) as db:
        # FTS via raw SQL — el GIN index ya existe (idx_transcriptions_text_fts).
        # listener inyecta WHERE user_id=X.
        result = (await db.execute(
            text("""
                SELECT id, original_filename, duration_seconds,
                       ts_rank(to_tsvector('spanish', text), plainto_tsquery('spanish', :q)) AS rank,
                       ts_headline('spanish', text, plainto_tsquery('spanish', :q),
                                   'MaxWords=20, MinWords=5, ShortWord=3, HighlightAll=false') AS snippet
                FROM transcriptions
                WHERE deleted_at IS NULL
                  AND to_tsvector('spanish', text) @@ plainto_tsquery('spanish', :q)
                ORDER BY rank DESC
                LIMIT :limit
            """),
            {"q": query, "limit": limit},
        )).all()

        return [{
            "id": str(r.id),
            "original_filename": r.original_filename,
            "duration_seconds": float(r.duration_seconds),
            "rank": float(r.rank),
            "snippet": r.snippet,
        } for r in result]
```

**Commits**: `test(mcp): SPEC-capa4 AC-4 — RED test FTS search`, `feat(mcp): SPEC-capa4 AC-4 — search_my_transcriptions FTS`.

#### Task 4.3 — RED + GREEN: `get_transcription` + cross-user 404

```python
async def test_get_transcription_own_returns_full(...):
async def test_get_transcription_other_user_returns_404(...):
    """AC-5: same shape as nonexistent id (no leak)."""
async def test_get_transcription_soft_deleted_returns_404(...):

# impl
@mcp_server.tool(name="get_transcription")
async def get_transcription(transcription_id: str) -> dict:
    user_id = get_current_user_id()
    try:
        tid = UUID(transcription_id)
    except ValueError:
        raise ToolError({"error_code": "INVALID_PARAMETER", "reason": "not a uuid"}, 400)

    async with mcp_request_session(user_id) as db:
        row = (await db.execute(
            select(Transcription).where(
                Transcription.id == tid,
                Transcription.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if row is None:
            raise ToolError({"error_code": "TRANSCRIPTION_NOT_FOUND", "reason": "not found"}, 404)
        images = (await db.execute(
            select(Image).where(
                Image.transcription_id == tid,
                Image.deleted_at.is_(None),
            )
        )).scalars().all()
        return _serialize_full(row, images)
```

**Commits**: `test(mcp): SPEC-capa4 AC-5 — RED tests get_transcription`, `feat(mcp): SPEC-capa4 AC-5 — get_transcription`.

---

### Batch 5 — Resources + delete + user_info

#### Task 5.1 — Resources `transcription://<id>` + `transcription://<id>/images/<image_id>`

```python
# src/transcription_api/mcp/resources.py
@mcp_server.resource("transcription://{transcription_id}")
async def transcription_resource(transcription_id: str) -> dict:
    return await get_transcription(transcription_id)  # reuse


@mcp_server.resource("transcription://{transcription_id}/images/{image_id}")
async def image_resource(transcription_id: str, image_id: str) -> bytes:
    user_id = get_current_user_id()
    tid = UUID(transcription_id)
    iid = UUID(image_id)
    async with mcp_request_session(user_id) as db:
        img = (await db.execute(
            select(Image).where(
                Image.id == iid,
                Image.transcription_id == tid,
                Image.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if img is None:
            raise ToolError({"error_code": "IMAGE_NOT_FOUND", "reason": "not found"}, 404)
        binary = Path(img.file_path).read_bytes()
        return MCPResource(content=binary, mime_type=img.mime_type, metadata={"caption": img.caption})
```

**Commits**: `test(mcp): SPEC-capa4 AC-6 + AC-7 — RED tests resources`, `feat(mcp): SPEC-capa4 AC-6 + AC-7 — resources`.

#### Task 5.2 — `delete_transcription` (soft delete + cascade)

```python
async def test_delete_own_marks_deleted_at(...):
async def test_delete_idempotent_returns_404(...):
async def test_delete_other_user_returns_404(...):

@mcp_server.tool(name="delete_transcription")
async def delete_transcription(transcription_id: str) -> dict:
    user_id = get_current_user_id()
    tid = UUID(transcription_id)
    async with mcp_request_session(user_id) as db:
        result = await db.execute(
            update(Transcription)
            .where(Transcription.id == tid, Transcription.deleted_at.is_(None))
            .values(deleted_at=func.now())
        )
        if result.rowcount == 0:
            raise ToolError({"error_code": "TRANSCRIPTION_NOT_FOUND", "reason": "not found"}, 404)
        await db.execute(
            update(Image)
            .where(Image.transcription_id == tid, Image.deleted_at.is_(None))
            .values(deleted_at=func.now())
        )
        return {"ok": True}
```

**Commits**: `test(mcp): SPEC-capa4 AC-11 — RED tests delete_transcription`, `feat(mcp): SPEC-capa4 AC-11 — delete + cascade`.

#### Task 5.3 — `get_user_info`

Trivial: SELECT users WHERE id=user_id (necesita bypass_scoping para que el listener no filtre el SELF — listener usa el `users` excluido por design ADR-014/015 §"users excluida del scoping" pero verify with test).

```python
@mcp_server.tool(name="get_user_info")
async def get_user_info() -> dict:
    user_id = get_current_user_id()
    bearer_id = _current_bearer_id.get()
    async with mcp_request_session(user_id) as db:
        with bypass_scoping(db):  # users no se scopeea por user_id
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        return {
            "user_id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
            "bearer_id": str(bearer_id),
        }
```

**Commits**: `test(mcp): SPEC-capa4 — RED test get_user_info`, `feat(mcp): SPEC-capa4 — get_user_info`.

---

### Batch 6 — Legacy deprecation + smoke E2E

#### Task 6.1 — Mark `POST /api/transcriptions` as `deprecated=True` + log WARN (AC-16)

**Files**:
- `src/transcription_api/api/transcriptions.py` (modificar): `@router.post("/transcriptions", deprecated=True)` + `logger.warning("legacy_endpoint_invoked ...")` al inicio del handler.
- `tests/integration/api/test_transcriptions.py` (extender RED): assert `app.openapi()["paths"]["/api/transcriptions"]["post"]["deprecated"] is True`.

**Commits**:
- `test(api): SPEC-capa4 AC-16 — RED test deprecation flag`.
- `chore(api): SPEC-capa4 AC-16 — mark POST /api/transcriptions deprecated`.

#### Task 6.2 — E2E rig smoke checklist + procedure doc

**File**: `docs/sesiones/2026-05-06-capa4-rig-smoke.md` (nuevo, template para Franco).

Contenido:
- Pre-checks: `alembic upgrade head` corrido, `\d upload_sessions` muestra `upload_bearer_hash`.
- Configuración Claude Code: agregar al `~/.claude/mcp.json` el server con la URL `http://<rig>/mcp` + bearer dev.
- Pasos: invocar `request_upload_url(kind=audio, file_size_bytes=<size>)` → curl multipart al upload_url con bearer ephemeral → `start_transcription(upload_id)` → `get_transcription(transcription_id)` → `list_my_transcriptions()` → `search_my_transcriptions(query=palabra-real)`.
- Asserts: cross-user con un segundo bearer (simulado).
- Cleanup: `delete_transcription(id)`.

**Commit**: `docs(capa4): SPEC-capa4 — rig smoke checklist for E2E`.

---

## Traceability matrix

| AC | Batch.Task | Test file | Commit (post-impl) |
|---|---|---|---|
| AC-1 | B2.1, B2.4, B3.1 | tests/integration/mcp/test_request_upload_url.py (9 tests) + tests/integration/api/test_upload.py (7 tests) + tests/integration/mcp/test_start_transcription.py (10 tests: 5 happy/lock/timeout/models + 5 edge cases) | RED `b50f1ba`+`e5df696`+`b542db9`+`b1ed27d` / GREEN `75a3f66`+`63ded4f`+`1d1600b` (full chain B2+B3 closed) |
| AC-2 | B3.3 | tests/integration/mcp/test_start_transcription.py::test_start_transcription_cross_user_returns_not_found (listener fail-closed AND-injects user_id; cross-user upload_id surfaces UPLOAD_SESSION_NOT_FOUND, no existence leak) | RED `b1ed27d` / GREEN `1d1600b` |
| AC-3 | B4.1 | tests/integration/mcp/test_list_my_transcriptions.py (5 tests: cross-user isolation + pagination + limit clamp + soft-delete filter + sort whitelist) | RED `772799f` / GREEN `a4e92f8` |
| AC-4 | B4.2 | tests/integration/mcp/test_search_my_transcriptions.py (5 tests: FTS rank+snippet + cross-user + empty/oversized query + limit clamp) | RED `1ea3ef0` / GREEN `3befcf2` |
| AC-5 | B4.3 | tests/integration/mcp/test_get_transcription.py (5 tests: own full payload + cross-user 404 + unknown 404 + soft-deleted 404 + invalid uuid) | RED `677645d` / GREEN `0d58662` |
| AC-6 | B5.1 | tests/integration/mcp/test_resources.py (transcription:// resource: full payload + cross-user 404) | RED `b8a5609` / GREEN `249ed8c` |
| AC-7 | B2.4 + B5.1 | tests/integration/mcp/test_resources.py (image_resource: bytes + cross-user/unknown/invalid-uuid -> IMAGE_NOT_FOUND/INVALID_PARAMETER) | RED `b8a5609` / GREEN `249ed8c` |
| AC-8 | B1.3 + B1.4 | tests/integration/mcp/test_mcp_middleware.py (4 tests cubren no-header / malformed / unknown / revoked) | RED `a7eeb01` / GREEN `78c25dd` |
| AC-9 | B3.1 | tests/integration/mcp/test_start_transcription.py::test_start_transcription_gpu_busy_returns_lock_busy + ::test_start_transcription_pipeline_timeout_returns_typed_error (orchestrate raises GPUBusy / PipelineTimeout -> tool maps to LOCK_BUSY / PIPELINE_TIMEOUT with structured fields) | RED `b542db9` / GREEN `1d1600b` |
| AC-10 | B3.3 + B2.4 | tests/integration/api/test_upload.py (B2.4 side: unknown_nonce + expired_session) + tests/integration/mcp/test_start_transcription.py (B3.3 side: expired / status='requested' / unknown_id all -> UPLOAD_SESSION_NOT_FOUND) | RED `e5df696`+`b1ed27d` / GREEN `63ded4f`+`1d1600b` |
| AC-11 | B5.2 | tests/integration/mcp/test_delete_transcription.py (6 tests: own soft-delete + cascade images + idempotente NOT_FOUND + cross-user NOT_FOUND + unknown NOT_FOUND + invalid uuid) | RED `c3f7d91` / GREEN `4aa794c` |
| AC-12 | B1.1 | tests/unit/mcp/test_mcp_module_imports.py + tests/integration/mcp/test_mcp_mount.py | RED `a1c906d` / GREEN `3425713` |
| AC-13 | (verification only) | tests/integration/auth/test_me.py (existing) | |
| AC-14 | B1.4 | tests/integration/mcp/test_mcp_middleware.py::test_mcp_valid_bearer_passes_middleware_and_bumps_last_used_at | RED `a7eeb01` / GREEN `78c25dd` |
| AC-15 | B0.1, B0.2 | tests/integration/test_alembic.py::test_upload_sessions_has_upload_bearer_hash_after_upgrade | RED `388104e` / GREEN `bff8926` |
| AC-16 | B6.1 | tests/integration/api/test_legacy_deprecation.py::test_post_transcriptions_marked_deprecated_in_openapi (no-Docker; local RED proof) + tests/integration/api/test_transcriptions.py::test_post_transcriptions_emits_legacy_warn_on_invocation (requires_docker; rig CI) | RED `8e61875` / GREEN `daeede2` |

---

## Definition of Done

- [ ] 16/16 ACs verdes (`pytest tests/ -m "not e2e and not requires_docker_gpu" -v` corre y pasa).
- [ ] Lint: `.venv/bin/python -m ruff check src/transcription_api/mcp/ src/transcription_api/api/upload.py tests/` clean.
- [ ] Sin regresiones: `pytest tests/integration/test_alembic.py tests/integration/api/test_transcriptions.py tests/integration/auth/ tests/unit/` todo verde.
- [ ] Migration aplicada en rig: `\d upload_sessions` en psql muestra `upload_bearer_hash text NOT NULL`.
- [ ] Smoke E2E B6.2 corrido por Franco (audio real en rig + Claude Code) — checklist firmado.
- [ ] Drift `D-044-impl` marcado cerrado en `docs/sesiones/2026-05-05-wiki-drifts.md` con commit hash de B0.2.
- [ ] Multi-agent review pre-merge (Capa 4 review-fixes idéntico al patrón Capa 2/3).
- [ ] Branch `feat/capa4-mcp` mergeable (no conflictos con master).

---

## Squash message template (para merge a master cuando todo esté verde)

```
feat(capa4): MCP Server (Streamable HTTP) + chunked upload pattern (SPEC-capa4-mcp-v1)

Tools MCP (RF-MCP-01..10): request_upload_url, start_transcription,
list_my_transcriptions, search_my_transcriptions, get_transcription,
delete_transcription, get_user_info. Resources transcription://<id> y
transcription://<id>/images/<image_id> (RF-MCP-07/08). Auth middleware
RF-MCP-11 con bearer SHA-256 hash compare + last_used_at best-effort.

REST endpoints `POST /api/upload` y `POST /api/upload-image` (RF-MCP-03 +
RF-IMG) con bearer ephemeral validation contra upload_bearer_hash
(D-044-impl, columna nueva en upload_sessions).

Reuse de pipeline.orchestrator.orchestrate (Capa 3) sin tocar el primitive
del lock. ADR-015 fail-closed scoping reusado vía mcp_request_session
context manager que arma db.info["user_id"].

Legacy POST /api/transcriptions marcado deprecated=True (removal en Capa 5).

Refs: SPEC-capa4-mcp-v1, ADR-011 (MCP-first), ADR-013 (uploads bearer),
ADR-015 (scoping), drifts D-026, D-027, D-028, D-040, D-042, D-043, D-044.
```
