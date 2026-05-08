# Capa 4 Review Fixes — TDD Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Cerrar los 14 grupos del review multi-agente de Capa 4 (3 CRITICAL + 14 HIGH + 22 MEDIUM consolidados) con disciplina TDD RED → GREEN → COMMIT por task, dejando `feat/capa4-mcp` mergeable a master.

**Architecture:** Cada task lleva el ciclo TDD completo (test failing first, minimal impl, verify pass, atomic commit). Tasks dentro de un grupo son secuenciales; grupos respetan dependencias del §3 del plan fuente. Donde el grupo sea refactor mecánico (sin behavior change), el "test" es ejecución del suite existente con counts unchanged + grep verification.

**Tech Stack:** Python 3.10–3.11, FastAPI 0.115, SQLAlchemy 2.0 async + asyncpg, Alembic, pytest + pytest-asyncio + respx + testcontainers + asgi-lifespan, FastMCP `mcp[server]>=1.5,<2.0`, Postgres 16-alpine.

**Source plan**: `docs/sesiones/2026-05-06-capa4-review-fixes-plan.md` — group definitions, file paths, severity rationale. **Read it first** for any group's context before executing its tasks.

**Branch**: `feat/capa4-mcp` (HEAD `7a063f4`, 43 commits ahead de master).

**Working dir**: `/Users/francobertoldi/Documents/Sandinas/IA-Tasks/IA-Tasks-Investigación-Estrategia/transcription-api` (use absolute paths).

**Test command base**: `.venv/bin/python -m pytest <path> -q -m "not e2e and not requires_docker_gpu and not requires_ffmpeg"`. Skip `requires_docker` is expected on Mac CPU dev (resolved on rig CI).

**Lint command**: `.venv/bin/python -m ruff check src/ tests/`.

**Commit format**: `<type>(<scope>): SPEC-capa4 G<N> — <desc>` for review-fix commits. Example: `feat(api): SPEC-capa4 G1 — POST /api/upload-image with magic bytes validation`. **No `Co-Authored-By` lines** (attribution disabled globally per `~/.claude/settings.json`).

---

## Execution order

```
Tier 1 (BLOCKING merge — start here):
  Parallel start: G1 + G3 + G5 (no overlap)
  After Tier 1 parallel done: G2 → G4 (sequential)

Tier 2 (depends on Tier 1):
  G6 (after G2 + G4)
  G7, G9 (independent, parallel possible)
  G8 (after G6 — some hooks live in new files)
  G10 (independent, anytime)

Tier 3 (post-merge OK but blocks next capa):
  G11 (after G1 + G2 + G6)
  G12 (after G2 + G6)
  G13, G14 (independent, anytime)
```

---

# Tier 1 — Blocking merge

## G1 — Image upload pipeline missing (CRITICAL)

**Severity**: CRITICAL — confirmed by 3 review lenses (pr-test, architect, sandinas).

**Why**: `mcp/tools/upload.py:184` emits `upload_url=…/api/upload-image?session=…` but `api/upload.py` only mounts `/api/upload`. AC-7 (image resource fetch) cannot pass end-to-end. Cross-user image scenario undefined.

### Task G1.1 — RED test for `test_upload_image_happy` (audio/png)

**Files:**
- Create: `tests/integration/api/test_upload_image.py`

**Step 1: Write the failing test**

```python
# tests/integration/api/test_upload_image.py
"""POST /api/upload-image — RF-IMG-02 / RF-MCP-03 (image branch)."""
from __future__ import annotations

import hashlib
import io
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from transcription_api.main import app

pytestmark = pytest.mark.requires_docker


@pytest.mark.asyncio
async def test_upload_image_happy(active_image_session, png_bytes_one_pixel, db_session):
    """Bearer + nonce + valid PNG → 200 + row in `images` + bytes in blobs/."""
    plaintext_bearer, session_row = active_image_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/upload-image",
            params={"session": session_row.nonce},
            headers={"Authorization": f"Bearer {plaintext_bearer}"},
            files={"file": ("smoke.png", io.BytesIO(png_bytes_one_pixel), "image/png")},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "image_id" in body
    # Row in DB
    result = (await db_session.execute(
        text("SELECT id, file_path, mime_type FROM images WHERE id = :iid"),
        {"iid": body["image_id"]},
    )).first()
    assert result is not None
    assert result.mime_type == "image/png"
    # session bumped to uploaded
    s = (await db_session.execute(
        text("SELECT status FROM upload_sessions WHERE id = :sid"),
        {"sid": session_row.id},
    )).scalar_one()
    assert s == "uploaded"
```

Fixtures `active_image_session` (factory in `tests/factories.py`) and `png_bytes_one_pixel` are added in this task (1×1 PNG = 67 bytes hardcoded `b"\x89PNG..."`).

**Step 2: Run test to verify it fails**

```
.venv/bin/python -m pytest tests/integration/api/test_upload_image.py::test_upload_image_happy -v
```

Expected: FAIL with `404 Not Found` for `POST /api/upload-image` (route does not exist).

**Step 3: Commit RED**

```bash
git add tests/integration/api/test_upload_image.py
git commit -m "test(api): SPEC-capa4 G1 — RED test for POST /api/upload-image happy path"
```

### Task G1.2 — GREEN: implement `POST /api/upload-image` + lifespan mkdir(blobs_dir)

**Files:**
- Modify: `src/transcription_api/api/upload.py` (add `upload_image` handler, ~80 LOC)
- Modify: `src/transcription_api/main.py:62-64` (add `settings.blobs_dir.mkdir(...)`)

**Step 1: Add the lifespan mkdir**

```python
# src/transcription_api/main.py — after settings.uploads_dir.mkdir(...) line
settings.blobs_dir.mkdir(parents=True, exist_ok=True)
logger.info("blobs_dir_ready path=%s", settings.blobs_dir)
```

**Step 2: Implement endpoint per RF-IMG-02**

```python
# src/transcription_api/api/upload.py — append after upload_audio
import imghdr
from uuid import uuid4
from sqlalchemy import insert
from ..db.models import Image

_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


@router.post("/upload-image")
async def upload_image(
    request: Request,
    session: str = Query(..., min_length=1),
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
):
    """Image upload — RF-MCP-03 image branch + RF-IMG-02."""
    plaintext = parse_bearer(authorization)  # G7 helper; for G1 inline ok
    if plaintext is None:
        return _error_resp(401, "MCP_BEARER_INVALID", "missing or malformed Authorization header")

    received_hash = hashlib.sha256(plaintext.encode("ascii")).hexdigest()

    with bypass_scoping(db):
        row = (await db.execute(
            select(UploadSession).where(
                UploadSession.nonce == session,
                UploadSession.kind == "image",
            )
        )).scalar_one_or_none()
    if row is None:
        return _error_resp(404, "UPLOAD_SESSION_NOT_FOUND", "image upload session unknown")
    if row.status != "requested":
        return _error_resp(404, "UPLOAD_SESSION_NOT_FOUND", "image upload session unknown")
    if row.expires_at <= datetime.now(timezone.utc):
        return _error_resp(404, "UPLOAD_SESSION_NOT_FOUND", "image upload session expired")
    if not hmac.compare_digest(row.upload_bearer_hash, received_hash):
        return _error_resp(401, "MCP_BEARER_INVALID", "ephemeral bearer mismatch")

    # Read bytes (limit by expected_size_bytes * margin) and validate magic.
    max_bytes = int(row.expected_size_bytes * 1.05)
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        return _error_resp(413, "FILE_TOO_LARGE", f"upload exceeds {max_bytes} bytes")

    # Magic bytes validation
    detected_kind = imghdr.what(None, raw[:32])
    expected_ext = _IMAGE_EXT.get(row.expected_mime_type or "")
    if expected_ext is None or detected_kind != ("jpeg" if expected_ext == "jpg" else expected_ext):
        return _error_resp(400, "INVALID_FORMAT",
                           f"detected={detected_kind!r} expected={row.expected_mime_type!r}")

    # Insert image row + write to blobs/<user_id>/<transcription_id>/<image_id>.<ext>
    image_id = uuid4()
    blob_dir = settings.blobs_dir / str(row.user_id) / str(row.transcription_id)
    blob_dir.mkdir(parents=True, exist_ok=True)
    blob_path = blob_dir / f"{image_id}.{expected_ext}"
    blob_path.write_bytes(raw)

    with bypass_scoping(db):
        await db.execute(insert(Image).values(
            id=image_id,
            transcription_id=row.transcription_id,
            user_id=row.user_id,
            mime_type=row.expected_mime_type,
            original_filename=file.filename or f"image_{image_id}.{expected_ext}",
            file_path=str(blob_path),
            size_bytes=len(raw),
        ))
        await db.execute(
            UploadSession.__table__.update()
            .where(UploadSession.id == row.id)
            .values(status="uploaded", uploaded_at=datetime.now(timezone.utc))
        )
        await db.commit()

    logger.info(
        "image_uploaded user_id=%s upload_id=%s image_id=%s size=%d",
        row.user_id, row.id, image_id, len(raw),
    )
    return {"ok": True, "image_id": str(image_id)}
```

**Step 3: Run test to verify it passes**

```
.venv/bin/python -m pytest tests/integration/api/test_upload_image.py::test_upload_image_happy -v
```

Expected: PASS (skipped on Mac without Docker, runs on rig CI).

**Step 4: Lint**

```
.venv/bin/python -m ruff check src/transcription_api/api/upload.py src/transcription_api/main.py tests/integration/api/test_upload_image.py
```

Expected: All checks passed!

**Step 5: Commit GREEN**

```bash
git add src/transcription_api/api/upload.py src/transcription_api/main.py tests/integration/api/test_upload_image.py
git commit -m "feat(api): SPEC-capa4 G1 — POST /api/upload-image + lifespan mkdir(blobs_dir)"
```

### Task G1.3 — RED+GREEN: `test_upload_image_wrong_magic_bytes_returns_400`

**Files:**
- Modify: `tests/integration/api/test_upload_image.py` (append test)

**Step 1: Add test**

```python
@pytest.mark.asyncio
async def test_upload_image_wrong_magic_bytes_returns_400(active_image_session, db_session):
    """Body says JPEG bytes but mime is image/png → INVALID_FORMAT 400."""
    plaintext_bearer, session_row = active_image_session
    fake_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 60  # JPEG SOI marker
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/upload-image",
            params={"session": session_row.nonce},
            headers={"Authorization": f"Bearer {plaintext_bearer}"},
            files={"file": ("fake.png", io.BytesIO(fake_bytes), "image/png")},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "INVALID_FORMAT"
```

**Step 2: Run + commit (no GREEN needed — already implemented in G1.2)**

```bash
.venv/bin/python -m pytest tests/integration/api/test_upload_image.py -v
git add tests/integration/api/test_upload_image.py
git commit -m "test(api): SPEC-capa4 G1 — magic bytes mismatch returns INVALID_FORMAT"
```

### Task G1.4 — RED+GREEN: `test_upload_image_cross_user_returns_404`

**Files:**
- Modify: `tests/integration/api/test_upload_image.py` (append test)

**Step 1: Add test**

```python
@pytest.mark.asyncio
async def test_upload_image_cross_user_returns_404(
    active_image_session, png_bytes_one_pixel, other_user_bearer_plaintext,
):
    """Bearer of user B with session of user A → 401 (hash mismatch since other-user bearer)."""
    _, session_row = active_image_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/upload-image",
            params={"session": session_row.nonce},
            headers={"Authorization": f"Bearer {other_user_bearer_plaintext}"},
            files={"file": ("img.png", io.BytesIO(png_bytes_one_pixel), "image/png")},
        )
    # Strict: hash compare fails (this bearer is unknown for that nonce)
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "MCP_BEARER_INVALID"
```

**Step 2: Run + commit**

```bash
.venv/bin/python -m pytest tests/integration/api/test_upload_image.py -v
git add tests/integration/api/test_upload_image.py
git commit -m "test(api): SPEC-capa4 G1 — cross-user bearer mismatch returns 401"
```

### Task G1.5 — RED: image E2E wire (request_upload_url → POST → resource fetch)

**Files:**
- Create: `tests/integration/mcp/test_image_e2e.py`

**Step 1: Write the failing test**

```python
# tests/integration/mcp/test_image_e2e.py
"""End-to-end image flow: tool request_upload_url(image) → POST /api/upload-image →
resource transcription://<id>/images/<image_id>. AC-7 closure."""
from __future__ import annotations

import io
import pytest

pytestmark = pytest.mark.requires_docker


@pytest.mark.asyncio
async def test_image_e2e_full_chain(
    mcp_client_with_bearer, active_bearer_plaintext, my_transcription, png_bytes_one_pixel,
):
    """request_upload_url → upload → resource fetch returns same bytes."""
    client = mcp_client_with_bearer(active_bearer_plaintext)
    # 1. Tool: get upload URL
    tool_resp = await client.call_tool("request_upload_url", {
        "kind": "image",
        "file_size_bytes": len(png_bytes_one_pixel),
        "mime_type": "image/png",
        "transcription_id": str(my_transcription.id),
    })
    body = tool_resp.content[0].data
    upload_url = body["upload_url"]
    ephemeral = body["bearer"]

    # 2. POST the bytes
    from httpx import ASGITransport, AsyncClient
    from transcription_api.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        post_resp = await http.post(
            upload_url.replace("http://localhost:8000", ""),  # strip base
            headers={"Authorization": f"Bearer {ephemeral}"},
            files={"file": ("img.png", io.BytesIO(png_bytes_one_pixel), "image/png")},
        )
    assert post_resp.status_code == 200
    image_id = post_resp.json()["image_id"]

    # 3. Resource fetch
    res_resp = await client.read_resource(f"transcription://{my_transcription.id}/images/{image_id}")
    # SDK MCP returns bytes verbatim for binary resources
    assert res_resp.contents[0].blob == png_bytes_one_pixel
```

**Step 2: Run + commit**

```bash
.venv/bin/python -m pytest tests/integration/mcp/test_image_e2e.py -v
git add tests/integration/mcp/test_image_e2e.py
git commit -m "test(mcp): SPEC-capa4 G1 — image E2E wire (AC-7 closure)"
```

(Test será SKIPPED en Mac sin Docker; corre en CI con testcontainer + agrega closure de AC-7.)

---

## G3 — FTS regconfig binding (Performance)

**Severity**: HIGH (sandinas-only finding).

**Why**: `func.to_tsvector("spanish", ...)` emite el literal como texto en lugar de `regconfig`, lo que hace que Postgres pueda no usar el índice GIN `idx_transcriptions_text_fts`. Performance regression silente.

### Task G3.1 — RED: regression test asserts EXPLAIN plan uses GIN index

**Files:**
- Modify: `tests/integration/mcp/test_search_my_transcriptions.py` (append test)

**Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_search_uses_gin_index_explain(my_user, db_session, mcp_client_with_bearer, active_bearer_plaintext):
    """AC-4 perf: search query EXPLAIN plan must show GIN index scan, not seq scan."""
    # Seed 100 rows so seq scan is a real cost (planner only picks index on volume)
    from tests.factories import make_transcription
    for i in range(100):
        await make_transcription(db_session, user_id=my_user.id,
                                  text_content=f"texto técnico arquitectura microservicios {i}")
    await db_session.commit()

    # Use raw SQL to capture the same predicate the tool builds
    from sqlalchemy import text
    explain = (await db_session.execute(text(
        "EXPLAIN (FORMAT JSON) "
        "SELECT id FROM transcriptions "
        "WHERE deleted_at IS NULL "
        "AND user_id = :uid "
        "AND to_tsvector('spanish'::regconfig, text) @@ plainto_tsquery('spanish'::regconfig, :q) "
        "ORDER BY ts_rank(to_tsvector('spanish'::regconfig, text), plainto_tsquery('spanish'::regconfig, :q)) DESC "
        "LIMIT 50"
    ), {"uid": my_user.id, "q": "arquitectura"})).scalar()
    plan = explain[0]["Plan"]
    plan_str = str(plan)
    assert "Bitmap Index Scan" in plan_str or "Index Scan" in plan_str, plan_str
    assert "idx_transcriptions_text_fts" in plan_str, plan_str
```

**Step 2: Run test (should pass with raw SQL but fail when tool's ORM expression is checked)**

The above test passes today because it bypasses the tool. Add a second test that runs the actual tool query and asserts on the planner — too involved. Instead: simplify with a string-equality check on the rendered SQL.

```python
def test_search_query_renders_with_regconfig_cast():
    """ORM expression for FTS must compile to use 'spanish'::regconfig (literal cast),
    not just the bare string 'spanish'. Without the cast, planner does not pick the
    GIN functional index."""
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql
    from transcription_api.db.models import Transcription
    from transcription_api.mcp.tools.transcription import _build_fts_predicate  # to add in G3.2

    stmt = select(Transcription.id).where(_build_fts_predicate(Transcription, "arquitectura"))
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "::regconfig" in sql, sql
```

**Step 3: Commit RED**

```bash
.venv/bin/python -m pytest tests/integration/mcp/test_search_my_transcriptions.py::test_search_query_renders_with_regconfig_cast -v
# Expected: FAIL — _build_fts_predicate doesn't exist yet
git add tests/integration/mcp/test_search_my_transcriptions.py
git commit -m "test(mcp): SPEC-capa4 G3 — RED test FTS query must cast 'spanish'::regconfig"
```

### Task G3.2 — GREEN: literal_column REGCONFIG + extract `_build_fts_predicate`

**Files:**
- Modify: `src/transcription_api/mcp/tools/transcription.py:355-402`
- Modify: `src/transcription_api/config.py` (add `fts_config: str = "spanish"`)

**Step 1: Implement helper + replace call sites**

```python
# src/transcription_api/mcp/tools/transcription.py — replace _FTS_CONFIG and uses
from sqlalchemy import literal_column, func
from ..config import settings

_FTS_REGCONFIG = literal_column(f"'{settings.fts_config}'::regconfig")


def _to_tsvector(col):
    return func.to_tsvector(_FTS_REGCONFIG, col)


def _to_tsquery(query: str):
    return func.plainto_tsquery(_FTS_REGCONFIG, query)


def _build_fts_predicate(model, query: str):
    """Return the WHERE expression that uses the GIN functional index."""
    return _to_tsvector(model.text_content).op("@@")(_to_tsquery(query))


def _ts_headline(query: str):
    return func.ts_headline(_FTS_REGCONFIG, Transcription.text_content, _to_tsquery(query),
                            "MaxFragments=2, MaxWords=20, MinWords=5")
```

Replace all uses of `func.to_tsvector("spanish", ...)` etc. with these helpers.

**Step 2: Add config**

```python
# src/transcription_api/config.py — append in Settings
fts_config: str = "spanish"
```

**Step 3: Run + commit**

```bash
.venv/bin/python -m pytest tests/integration/mcp/test_search_my_transcriptions.py -v
.venv/bin/python -m ruff check src/transcription_api/mcp/tools/transcription.py src/transcription_api/config.py
git add src/transcription_api/mcp/tools/transcription.py src/transcription_api/config.py
git commit -m "feat(mcp): SPEC-capa4 G3 — FTS uses 'spanish'::regconfig literal for GIN hit"
```

---

## G5 — `list/search` params hardening

**Severity**: HIGH (silent-failure + code-reviewer + pr-test).

**Why**: actual implementation silently clamps any out-of-range value; gives no signal to the caller. Tautological tests pass. Need raise vs clamp policy + non-tautological tests.

### Task G5.1 — RED: invalid params raise INVALID_PARAMETER

**Files:**
- Modify: `tests/integration/mcp/test_search_my_transcriptions.py` (append tests)
- Modify: `tests/integration/mcp/test_list_my_transcriptions.py` (append tests)

**Step 1: Write failing tests**

```python
# test_search_my_transcriptions.py
@pytest.mark.parametrize("bad_input,reason", [
    ({"query": "foo", "limit": -1}, "limit < 1"),
    ({"query": "foo", "limit": 0}, "limit < 1"),
    ({"query": "", "limit": 10}, "empty query"),
    ({"query": "foo", "limit": 9999}, "limit > MAX*2"),
])
async def test_search_invalid_params_raise(mcp_client_with_bearer, active_bearer_plaintext, bad_input, reason):
    client = mcp_client_with_bearer(active_bearer_plaintext)
    resp = await client.call_tool("search_my_transcriptions", bad_input)
    assert resp.is_error
    assert resp.error.data["error_code"] == "INVALID_PARAMETER", reason


@pytest.mark.asyncio
async def test_search_clamp_silent_in_grace_window(mcp_client_with_bearer, active_bearer_plaintext, my_user, db_session):
    """limit=80 (between MAX=50 and MAX*2=100) → clamp silently to 50."""
    from tests.factories import make_transcription
    for i in range(60):
        await make_transcription(db_session, user_id=my_user.id, text_content=f"arquitectura {i}")
    await db_session.commit()
    client = mcp_client_with_bearer(active_bearer_plaintext)
    resp = await client.call_tool("search_my_transcriptions", {"query": "arquitectura", "limit": 80})
    assert not resp.is_error
    assert len(resp.content[0].data) == 50  # clamped to MAX
```

```python
# test_list_my_transcriptions.py
@pytest.mark.parametrize("bad_input", [
    {"limit": -1, "offset": 0},
    {"limit": 0, "offset": 0},
    {"limit": 10, "offset": -1},
    {"limit": 999, "offset": 0},  # > MAX*2 = 200
])
async def test_list_invalid_params_raise(mcp_client_with_bearer, active_bearer_plaintext, bad_input):
    client = mcp_client_with_bearer(active_bearer_plaintext)
    resp = await client.call_tool("list_my_transcriptions", bad_input)
    assert resp.is_error
    assert resp.error.data["error_code"] == "INVALID_PARAMETER"
```

**Step 2: Commit RED**

```bash
git add tests/integration/mcp/test_search_my_transcriptions.py tests/integration/mcp/test_list_my_transcriptions.py
git commit -m "test(mcp): SPEC-capa4 G5 — RED tests for invalid/clamp params policy"
```

### Task G5.2 — GREEN: `_clamp` helper + validation policy

**Files:**
- Create: `src/transcription_api/mcp/_clamp.py`
- Modify: `src/transcription_api/mcp/tools/transcription.py` (list_my_, search_my_)

**Step 1: Helper**

```python
# src/transcription_api/mcp/_clamp.py
"""Param clamp policy: raise on clearly-bug input, clamp silently in grace window,
raise on absurd values."""
from .errors import raise_tool_error


def clamp_or_raise(value: int, *, lo: int, hi: int, name: str) -> int:
    """Validate and clamp `value` for limit/offset-style integers.

    - value < lo → raise INVALID_PARAMETER (clearly client bug)
    - lo <= value <= hi → return value
    - hi < value <= hi*2 → clamp silently to hi (lenient grace)
    - value > hi*2 → raise INVALID_PARAMETER (absurd value, client error)
    """
    if value < lo:
        raise_tool_error("INVALID_PARAMETER", f"{name} must be >= {lo}", 400, extra={"min": lo})
    if value <= hi:
        return value
    if value <= hi * 2:
        return hi  # silent clamp, grace window
    raise_tool_error("INVALID_PARAMETER", f"{name} must be <= {hi}", 400, extra={"max_limit": hi})
```

**Step 2: Apply in tools**

```python
# transcription.py — replace inline clamps
from .._clamp import clamp_or_raise

_LIST_LIMIT_MAX = 100
_SEARCH_LIMIT_MAX = 50


@mcp_server.tool(name="list_my_transcriptions")
async def list_my_transcriptions(limit: int = 20, offset: int = 0, sort: str = "created_at_desc"):
    limit = clamp_or_raise(limit, lo=1, hi=_LIST_LIMIT_MAX, name="limit")
    if offset < 0:
        raise_tool_error("INVALID_PARAMETER", "offset must be >= 0", 400, extra={"min": 0})
    # ... rest unchanged


@mcp_server.tool(name="search_my_transcriptions")
async def search_my_transcriptions(query: str, limit: int = 10):
    if not query or not query.strip():
        raise_tool_error("INVALID_PARAMETER", "query must be non-empty", 400)
    limit = clamp_or_raise(limit, lo=1, hi=_SEARCH_LIMIT_MAX, name="limit")
    # ... rest unchanged
```

**Step 3: Run + commit**

```bash
.venv/bin/python -m pytest tests/integration/mcp/test_search_my_transcriptions.py tests/integration/mcp/test_list_my_transcriptions.py -v
.venv/bin/python -m ruff check src/transcription_api/mcp/
git add src/transcription_api/mcp/_clamp.py src/transcription_api/mcp/tools/transcription.py
git commit -m "feat(mcp): SPEC-capa4 G5 — clamp_or_raise policy + helper"
```

---

## G2 — `start_transcription` extract + cleanup en finally

**Severity**: HIGH (4 findings consolidated). Sequential after G1+G3+G5.

**Why**: 155 LOC in single function, `_models_loaded_or_503` duplicated between REST and MCP, cleanup not in finally → orphan dirs on orchestrate failure, kwargs not asserted in tests.

### Task G2.1 — RED: orphan dir test for orchestrate failure

**Files:**
- Modify: `tests/integration/mcp/test_start_transcription.py` (append test)

**Step 1: Write test**

```python
@pytest.mark.asyncio
async def test_start_transcription_cleans_upload_dir_on_orchestrate_failure(
    mcp_client_with_bearer, active_bearer_plaintext, uploaded_audio_session, monkeypatch, tmp_uploads,
):
    """If orchestrate raises, the upload_dir must NOT linger on disk."""
    from unittest.mock import AsyncMock
    monkeypatch.setattr(
        "transcription_api.mcp.tools.transcription.orchestrate",
        AsyncMock(side_effect=RuntimeError("simulated GPU crash")),
    )
    upload_dir = tmp_uploads / str(uploaded_audio_session.id)
    assert upload_dir.exists()  # sanity: fixture created it

    client = mcp_client_with_bearer(active_bearer_plaintext)
    resp = await client.call_tool("start_transcription", {"upload_id": str(uploaded_audio_session.id)})
    assert resp.is_error  # the error propagates as MCP error
    assert not upload_dir.exists(), "upload dir leaked after orchestrate failure"
```

**Step 2: Commit RED**

```bash
.venv/bin/python -m pytest tests/integration/mcp/test_start_transcription.py::test_start_transcription_cleans_upload_dir_on_orchestrate_failure -v
# Expected: FAIL — upload_dir leaks because cleanup is post-orchestrate, not in finally
git add tests/integration/mcp/test_start_transcription.py
git commit -m "test(mcp): SPEC-capa4 G2 — RED test orphan upload_dir on orchestrate failure"
```

### Task G2.2 — GREEN: extract `_models_ready_or_raise`

**Files:**
- Create: `src/transcription_api/runtime/__init__.py`
- Create: `src/transcription_api/runtime/readiness.py`
- Modify: `src/transcription_api/api/transcriptions.py` (use shared helper)
- Modify: `src/transcription_api/mcp/tools/transcription.py` (use shared helper)

**Step 1: Create the helper**

```python
# src/transcription_api/runtime/readiness.py
"""Centralized readiness gate for whisper/pyannote models.

Both POST /api/transcriptions and start_transcription tool need to know if
the lifespan-loaded models are ready. Duplicated logic was diverging.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    failing_model: str | None  # "whisper" | "pyannote" | None
    detail: str | None


def check_models_ready(app_state) -> ReadinessResult:
    """Inspect lifespan-armed app.state. Raise if state was never armed
    (lifespan bug — louder than ambiguous "loading")."""
    if not hasattr(app_state, "whisper_status"):
        raise RuntimeError(
            "LIFESPAN_DID_NOT_ARM_STATE: app.state.whisper_status missing. "
            "This indicates the FastAPI lifespan never ran or crashed early."
        )
    whisper = app_state.whisper_status
    pyannote = app_state.pyannote_status
    if whisper == "ready" and pyannote == "ready":
        return ReadinessResult(ready=True, failing_model=None, detail=None)
    if whisper != "ready":
        return ReadinessResult(ready=False, failing_model="whisper",
                               detail=getattr(app_state, "whisper_detail", None))
    return ReadinessResult(ready=False, failing_model="pyannote",
                           detail=getattr(app_state, "pyannote_detail", None))
```

**Step 2: Use in both call sites**

```python
# api/transcriptions.py — replace _models_loaded_or_503
from ..runtime.readiness import check_models_ready


def _models_loaded_or_503(request):
    res = check_models_ready(request.app.state)
    if res.ready:
        return None
    return JSONResponse(status_code=503, headers={"Retry-After": "30"}, content={
        "detail": {"error_code": "MODELS_NOT_LOADED",
                   "reason": f"{res.failing_model} model is not ready",
                   "detail": res.detail}})
```

```python
# mcp/tools/transcription.py — in start_transcription
from ...runtime.readiness import check_models_ready

# inside start_transcription, before orchestrate call:
res = check_models_ready(_get_app_state())
if not res.ready:
    raise_tool_error("MODELS_NOT_LOADED",
                     f"{res.failing_model} model is not ready",
                     503, extra={"detail": res.detail})
```

**Step 3: Run + commit**

```bash
.venv/bin/python -m pytest tests/integration/api/test_transcriptions.py tests/integration/mcp/test_start_transcription.py -v
git add src/transcription_api/runtime/ src/transcription_api/api/transcriptions.py src/transcription_api/mcp/tools/transcription.py
git commit -m "refactor(runtime): SPEC-capa4 G2 — extract check_models_ready (deduplicate gate)"
```

### Task G2.3 — GREEN: cleanup en `try/finally` + helpers split

**Files:**
- Modify: `src/transcription_api/mcp/tools/transcription.py:125-279`

**Step 1: Refactor `start_transcription` body**

```python
async def _load_upload_row(db, upload_id, user_id, *, grace_seconds: int = 30):
    """Resolve an uploaded session for this user. Raise typed errors for invalid states."""
    try:
        uid = UUID(upload_id)
    except ValueError:
        raise_tool_error("INVALID_PARAMETER", "upload_id is not a valid UUID", 400)
    row = (await db.execute(
        select(UploadSession).where(UploadSession.id == uid, UploadSession.kind == "audio")
    )).scalar_one_or_none()
    if row is None:
        raise_tool_error("UPLOAD_SESSION_NOT_FOUND", "upload not found or already consumed", 404)
    if row.status == "consumed":
        raise_tool_error("UPLOAD_SESSION_ALREADY_CONSUMED",
                         "this upload was already used by a previous start_transcription", 409)
    if row.status == "requested":
        raise_tool_error("UPLOAD_SESSION_NOT_FOUND", "upload not found or already consumed", 404)
    grace_cutoff = row.expires_at + timedelta(seconds=grace_seconds)
    if datetime.now(timezone.utc) > grace_cutoff:
        raise_tool_error("UPLOAD_SESSION_NOT_FOUND", "upload not found or already consumed", 404)
    return row


async def _consume_upload(db, row):
    await db.execute(
        UploadSession.__table__.update()
        .where(UploadSession.id == row.id)
        .values(status="consumed", consumed_at=datetime.now(timezone.utc))
    )


def _cleanup_upload_dir(upload_dir: Path) -> None:
    if upload_dir.exists():
        try:
            shutil.rmtree(upload_dir)
        except OSError as exc:
            logger.warning("upload_dir_cleanup_failed path=%s exc=%s", upload_dir, exc)


@mcp_server.tool(name="start_transcription")
async def start_transcription(upload_id: str, language: str = "es",
                               min_speakers: int = 1, max_speakers: int = 8) -> dict:
    user_id = get_current_user_id()
    res = check_models_ready(_get_app_state())
    if not res.ready:
        raise_tool_error("MODELS_NOT_LOADED",
                         f"{res.failing_model} model is not ready",
                         503, extra={"detail": res.detail})

    async with mcp_request_session(user_id) as db:
        row = await _load_upload_row(db, upload_id, user_id)
        upload_dir = settings.uploads_dir / str(row.id)

        try:
            file_path = upload_dir / "original.bin"
            if not file_path.exists():
                raise_tool_error("UPLOAD_SESSION_NOT_FOUND", "upload binary missing on disk", 404)

            try:
                result = await orchestrate(
                    user_id=user_id, db=db, file_path=file_path,
                    original_filename=row.expected_mime_type or "audio.bin",
                    original_size_bytes=row.expected_size_bytes,
                    whisper_model=_get_app_state().whisper_model,
                    pyannote_pipeline=_get_app_state().pyannote_pipeline,
                    cache_store=CacheStore(base_dir=settings.cache_dir),
                    upload_dir=upload_dir, language=language,
                    min_speakers=min_speakers, max_speakers=max_speakers,
                )
            except GPUBusy as e:
                raise_tool_error("LOCK_BUSY", "GPU is busy with another job", 503,
                                 extra={"retry_after": e.retry_after})
            except PipelineTimeout as e:
                raise_tool_error("PIPELINE_TIMEOUT", "pipeline exceeded timeout", 504,
                                 extra={"timeout_seconds": e.timeout_seconds})

            await _consume_upload(db, row)
            return {
                "transcription_id": str(result["transcription_id"]),
                "status": "completed",
                "cache_hit": result.get("metadata", {}).get("cache_hit", False),
            }
        finally:
            _cleanup_upload_dir(upload_dir)
```

**Step 2: Run all tests + commit**

```bash
.venv/bin/python -m pytest tests/integration/mcp/test_start_transcription.py -v
.venv/bin/python -m ruff check src/transcription_api/mcp/
git add src/transcription_api/mcp/tools/transcription.py
git commit -m "refactor(mcp): SPEC-capa4 G2 — start_transcription split + cleanup en finally"
```

### Task G2.4 — GREEN: kwargs assertion in tests

**Files:**
- Modify: `tests/integration/mcp/test_start_transcription.py`

**Step 1: Strengthen happy-path test**

```python
@pytest.mark.asyncio
async def test_start_transcription_happy_passes_correct_kwargs_to_orchestrate(
    mcp_client_with_bearer, active_bearer_plaintext, uploaded_audio_session, monkeypatch, my_user,
):
    from unittest.mock import AsyncMock
    fake_result = {"transcription_id": uuid4(), "metadata": {"cache_hit": False}}
    mock_orchestrate = AsyncMock(return_value=fake_result)
    monkeypatch.setattr("transcription_api.mcp.tools.transcription.orchestrate", mock_orchestrate)

    client = mcp_client_with_bearer(active_bearer_plaintext)
    await client.call_tool("start_transcription", {
        "upload_id": str(uploaded_audio_session.id),
        "language": "en", "max_speakers": 4, "min_speakers": 2,
    })
    mock_orchestrate.assert_awaited_once()
    kwargs = mock_orchestrate.call_args.kwargs
    assert kwargs["user_id"] == my_user.id
    assert kwargs["language"] == "en"
    assert kwargs["max_speakers"] == 4
    assert kwargs["min_speakers"] == 2
    assert kwargs["whisper_model"] is not None
    assert kwargs["pyannote_pipeline"] is not None
    assert kwargs["cache_store"] is not None
```

**Step 2: Commit**

```bash
.venv/bin/python -m pytest tests/integration/mcp/test_start_transcription.py -v
git add tests/integration/mcp/test_start_transcription.py
git commit -m "test(mcp): SPEC-capa4 G2 — assert orchestrate kwargs explicit (no kwargs drift)"
```

---

## G4 — `lookup_owned_or_404` helper (privacy invariant)

**Severity**: HIGH (promoted from STRATEGIC — 3 agents converged).

**Why**: 5 sites copy-paste the same SELECT-or-404 pattern. A future change to one site that diverges from the privacy invariant ("collapse cross-user, unknown, soft-deleted to a single 404 with same body") is a leak waiting to happen.

### Task G4.1 — RED: helper unit test for collapse semantics

**Files:**
- Create: `tests/unit/mcp/test_lookup_helper.py`

**Step 1: Write test**

```python
# tests/unit/mcp/test_lookup_helper.py
"""lookup_owned_or_404 collapses (cross-user, unknown, soft-deleted) → 404."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from transcription_api.mcp.errors import McpError
from transcription_api.mcp.lookup import lookup_owned_or_404


class _FakeModel:
    id = "id_col"
    deleted_at = "del_col"


@pytest.mark.asyncio
async def test_lookup_returns_row_when_found():
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: "row_fake"))
    out = await lookup_owned_or_404(db, _FakeModel, "uuid", error_code="X", error_message="m")
    assert out == "row_fake"


@pytest.mark.asyncio
async def test_lookup_raises_404_when_not_found():
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))
    with pytest.raises(McpError) as exc:
        await lookup_owned_or_404(db, _FakeModel, "uuid",
                                  error_code="TRANSCRIPTION_NOT_FOUND", error_message="m")
    assert exc.value.error.data["error_code"] == "TRANSCRIPTION_NOT_FOUND"
```

**Step 2: Commit RED**

```bash
.venv/bin/python -m pytest tests/unit/mcp/test_lookup_helper.py -v
git add tests/unit/mcp/test_lookup_helper.py
git commit -m "test(mcp): SPEC-capa4 G4 — RED tests for lookup_owned_or_404 helper"
```

### Task G4.2 — GREEN: implement helper

**Files:**
- Create: `src/transcription_api/mcp/lookup.py`

**Step 1: Implement**

```python
# src/transcription_api/mcp/lookup.py
"""Privacy-preserving lookup helper.

`lookup_owned_or_404` SELECTs a per-user row through the scoping listener
(must run inside `mcp_request_session(user_id)`). All three failure causes
(cross-user, unknown id, soft-deleted) collapse to the SAME error code +
message. ADR-015 (fail-closed) + privacy invariant: no existence leak.
"""
from __future__ import annotations
from typing import Type, TypeVar
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .errors import raise_tool_error

T = TypeVar("T")


async def lookup_owned_or_404(
    db: AsyncSession, model: Type[T], id_value, *,
    error_code: str, error_message: str, soft_delete: bool = True,
) -> T:
    stmt = select(model).where(model.id == id_value)
    if soft_delete and hasattr(model, "deleted_at"):
        stmt = stmt.where(model.deleted_at.is_(None))
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise_tool_error(error_code, error_message, 404)
    return row  # type: ignore[return-value]
```

**Step 2: Commit GREEN**

```bash
.venv/bin/python -m pytest tests/unit/mcp/test_lookup_helper.py -v
.venv/bin/python -m ruff check src/transcription_api/mcp/lookup.py
git add src/transcription_api/mcp/lookup.py
git commit -m "feat(mcp): SPEC-capa4 G4 — lookup_owned_or_404 helper"
```

### Task G4.3 — REFACTOR: 5 call sites use the helper

**Files:**
- Modify: `src/transcription_api/mcp/tools/transcription.py` (get + delete + start_transcription)
- Modify: `src/transcription_api/mcp/tools/upload.py` (image transcription_id check)
- Modify: `src/transcription_api/mcp/resources.py` (image_resource)

**Step 1: Patch call sites**

Replace patterns like:
```python
row = (await db.execute(select(Transcription).where(Transcription.id == tid, Transcription.deleted_at.is_(None)))).scalar_one_or_none()
if row is None:
    raise_tool_error("TRANSCRIPTION_NOT_FOUND", "...", 404)
```
With:
```python
row = await lookup_owned_or_404(db, Transcription, tid,
                                 error_code="TRANSCRIPTION_NOT_FOUND",
                                 error_message="transcription not found")
```

**Step 2: Run full test suite + commit**

```bash
.venv/bin/python -m pytest tests/unit tests/integration -q -m "not e2e and not requires_docker_gpu and not requires_ffmpeg"
.venv/bin/python -m ruff check src/transcription_api/mcp/
git add src/transcription_api/mcp/tools/ src/transcription_api/mcp/resources.py
git commit -m "refactor(mcp): SPEC-capa4 G4 — 5 call sites use lookup_owned_or_404"
```

---

# Tier 2 — High value, no-block

## G6 — Tools file split + serializers module

**Severity**: HIGH (cohesion drift). Depends on G2 + G4 (cleaner code to split).

### Task G6.1 — Create `mcp/serializers.py`

**Files:**
- Create: `src/transcription_api/mcp/serializers.py`

**Step 1: Move serialization logic**

```python
# src/transcription_api/mcp/serializers.py
"""Pure functions: ORM row → MCP payload dict. Reusable from tools and resources."""
from typing import Any
from ..db.models import Image, Transcription


def serialize_summary(row: Transcription) -> dict[str, Any]:
    return {
        "id": str(row.id), "original_filename": row.original_filename,
        "duration_seconds": float(row.duration_seconds), "language": row.language,
        "num_speakers": row.num_speakers, "created_at": row.created_at.isoformat(),
    }


def serialize_full(row: Transcription, images: list[Image]) -> dict[str, Any]:
    return {
        **serialize_summary(row),
        "audio_hash": row.audio_hash, "text_content": row.text_content,
        "segments": unwrap_segments(row.segments),
        "metadata": row.extra_metadata or {},
        "images": [serialize_image(img) for img in images],
    }


def serialize_image(img: Image) -> dict[str, Any]:
    return {
        "id": str(img.id), "mime_type": img.mime_type,
        "original_filename": img.original_filename, "size_bytes": img.size_bytes,
    }


def unwrap_segments(blob: Any) -> list[dict[str, Any]]:
    """`segments` JSONB persists as `{"segments": [...]}`; unwrap to bare list."""
    if isinstance(blob, dict) and "segments" in blob:
        return blob["segments"]
    if isinstance(blob, list):
        return blob
    return []
```

**Step 2: Commit**

```bash
git add src/transcription_api/mcp/serializers.py
git commit -m "feat(mcp): SPEC-capa4 G6 — extract serializers to dedicated module"
```

### Task G6.2 — Split tools/transcription.py per verb

**Files:**
- Create: `src/transcription_api/mcp/tools/start.py` (start_transcription)
- Create: `src/transcription_api/mcp/tools/list.py` (list_my_transcriptions)
- Create: `src/transcription_api/mcp/tools/search.py` (search_my_transcriptions + FTS helpers)
- Create: `src/transcription_api/mcp/tools/get.py` (get_transcription)
- Create: `src/transcription_api/mcp/tools/delete.py` (delete_transcription)
- Create: `src/transcription_api/mcp/tools/user.py` (get_user_info)
- Delete: `src/transcription_api/mcp/tools/transcription.py`
- Modify: `src/transcription_api/mcp/tools/__init__.py` (import all 6 modules)

**Step 1: Move each tool to its own file**

Each new file ≤200 LOC. Imports use relative imports. Decorator `@mcp_server.tool(...)` runs at import time, so as long as `tools/__init__.py` imports each module, registration works.

**Step 2: Verify pytest unchanged**

```bash
.venv/bin/python -m pytest tests/integration/mcp -q -m "not requires_docker_gpu and not requires_ffmpeg"
.venv/bin/python -m ruff check src/transcription_api/mcp/
# Counts unchanged from before split
```

**Step 3: Commit**

```bash
git add src/transcription_api/mcp/tools/
git commit -m "refactor(mcp): SPEC-capa4 G6 — split tools/transcription.py per verb"
```

---

## G7 — Bearer hash + parser consolidation

**Severity**: MEDIUM. Independent of others.

### Task G7.1 — Create `auth/header.py::parse_bearer`

**Files:**
- Create: `src/transcription_api/auth/header.py`

**Step 1: Implement**

```python
# src/transcription_api/auth/header.py
"""Authorization: Bearer <token> parser. Single source for the format check."""
def parse_bearer(authorization: str | None) -> str | None:
    """Return the plaintext token, or None if header is absent/malformed."""
    if not authorization:
        return None
    prefix, _, token = authorization.partition(" ")
    if prefix.lower() != "bearer" or not token.strip():
        return None
    return token.strip()
```

**Step 2: Commit**

```bash
git add src/transcription_api/auth/header.py
git commit -m "feat(auth): SPEC-capa4 G7 — parse_bearer helper"
```

### Task G7.2 — Replace inline hash + parser usages

**Files:**
- Modify: `src/transcription_api/mcp/middleware.py` (use parse_bearer + hash_bearer)
- Modify: `src/transcription_api/api/upload.py` (use parse_bearer + hash_bearer)
- Modify: `src/transcription_api/mcp/tools/upload.py` (use hash_bearer in image branch)

**Step 1: Patch each call site to use the helpers**

Replace `sha256(plaintext.encode("ascii")).hexdigest()` → `hash_bearer(plaintext)`.
Replace bearer-prefix string ops → `parse_bearer(authorization)`.

**Step 2: Verify + commit**

```bash
.venv/bin/python -m pytest tests/integration -q
grep -rn "sha256(.*encode.*ascii" src/  # should match only auth/mcp_bearer.py
git add src/transcription_api/mcp/middleware.py src/transcription_api/api/upload.py src/transcription_api/mcp/tools/upload.py
git commit -m "refactor(auth): SPEC-capa4 G7 — consolidate bearer hash + header parsing"
```

---

## G8 — Type / correctness microfixes

**Severity**: MEDIUM (lote). Depends on G6.

### Task G8.1 — `raise_tool_error -> NoReturn`

**Files:**
- Modify: `src/transcription_api/mcp/errors.py`

**Step 1: Annotate**

```python
from typing import NoReturn

def raise_tool_error(code: str, reason: str, http_status: int, extra: dict | None = None) -> NoReturn:
    ...
    raise McpError(...)
```

**Step 2: Commit**

```bash
git add src/transcription_api/mcp/errors.py
git commit -m "fix(mcp): SPEC-capa4 G8 — raise_tool_error annotated NoReturn"
```

### Task G8.2 — `_resolve_user_id` type guard

**Files:**
- Modify: `src/transcription_api/db/scoping.py:124-126`

**Step 1: Add isinstance check**

```python
def _resolve_user_id(state: ORMExecuteState) -> uuid.UUID | None:
    info = state.session.info
    user_id = info.get("user_id")
    if user_id is not None and not isinstance(user_id, uuid.UUID):
        raise ScopingNotArmedError(
            f"session.info['user_id'] is {type(user_id).__name__!r}, expected uuid.UUID"
        )
    return user_id
```

**Step 2: RED test**

```python
# tests/unit/db/test_scoping_resolve_user_id.py — new
import pytest
from transcription_api.db.scoping import _resolve_user_id, ScopingNotArmedError

def test_resolve_user_id_raises_on_non_uuid():
    state = MagicMock(); state.session.info = {"user_id": "not-a-uuid"}
    with pytest.raises(ScopingNotArmedError, match="expected uuid.UUID"):
        _resolve_user_id(state)
```

**Step 3: Commit**

```bash
.venv/bin/python -m pytest tests/unit/db -v
git add src/transcription_api/db/scoping.py tests/unit/db/test_scoping_resolve_user_id.py
git commit -m "fix(db): SPEC-capa4 G8 — _resolve_user_id raises on non-UUID type"
```

### Task G8.3 — `unwrap_segments` handles bare list

**Files:**
- Modify: `src/transcription_api/mcp/serializers.py` (already done in G6.1 — add test)
- Create: `tests/unit/mcp/test_serializers.py`

**Step 1: Test**

```python
def test_unwrap_segments_handles_bare_list():
    from transcription_api.mcp.serializers import unwrap_segments
    assert unwrap_segments([{"start": 0}]) == [{"start": 0}]
    assert unwrap_segments({"segments": [{"start": 0}]}) == [{"start": 0}]
    assert unwrap_segments(None) == []
```

**Step 2: Commit**

```bash
.venv/bin/python -m pytest tests/unit/mcp/test_serializers.py -v
git add tests/unit/mcp/test_serializers.py
git commit -m "test(mcp): SPEC-capa4 G8 — unwrap_segments handles list/dict/None"
```

### Task G8.4 — `expires_at` grace seconds

**Files:**
- Modify: `src/transcription_api/config.py` (add `upload_session_grace_seconds: int = 30`)
- Modify: `src/transcription_api/mcp/tools/start.py` (use grace in `_load_upload_row`)
- Modify: `src/transcription_api/api/upload.py` (use grace in upload audio + image)

**Step 1: Patch**

```python
# config.py
upload_session_grace_seconds: int = 30  # RF-MCP-02 step 6 grace window after expires_at

# tools/start.py - already done in G2.3 with timedelta(seconds=30); now read from config
grace = settings.upload_session_grace_seconds
grace_cutoff = row.expires_at + timedelta(seconds=grace)
```

**Step 2: Test**

```python
# tests/integration/mcp/test_start_transcription.py — append
@pytest.mark.parametrize("offset_sec,expect_404", [
    (-5, False),    # within window
    (29, False),    # within grace
    (31, True),     # past grace
])
async def test_upload_session_grace_window(offset_sec, expect_404, ...):
    # set expires_at = now + offset_sec
    ...
```

**Step 3: Commit**

```bash
git add src/transcription_api/config.py src/transcription_api/mcp/tools/start.py src/transcription_api/api/upload.py tests/integration/mcp/test_start_transcription.py
git commit -m "feat(config): SPEC-capa4 G8 — upload_session_grace_seconds (RF-MCP-02 step 6)"
```

### Task G8.5 — Log delete cascade rowcount

**Files:**
- Modify: `src/transcription_api/mcp/tools/delete.py`

**Step 1: Patch**

```python
result = await db.execute(update(Image).where(...).values(deleted_at=...))
logger.info("delete_cascade_images user_id=%s transcription_id=%s rowcount=%d",
            user_id, tid, result.rowcount)
```

**Step 2: Commit**

```bash
git add src/transcription_api/mcp/tools/delete.py
git commit -m "feat(mcp): SPEC-capa4 G8 — log delete cascade image rowcount"
```

---

## G9 — `mcp_request_session` relocation + bypass tightening

**Severity**: MEDIUM. Independent.

### Task G9.1 — Move ctx mgr to `db/session.py`

**Files:**
- Modify: `src/transcription_api/db/session.py` (add `scoped_session` ctx mgr)
- Modify: `src/transcription_api/mcp/session.py` (re-export)

**Step 1: Move body**

```python
# db/session.py — append
from contextlib import asynccontextmanager
from uuid import UUID
from .scoping import set_session_user

@asynccontextmanager
async def scoped_session(user_id: UUID | None):
    """Open an AsyncSession with `info['user_id']` armed (or None for explicit
    no-arming → listener fail-closes on per-user queries)."""
    async with async_session_factory() as session:
        if user_id is not None:
            set_session_user(session, user_id)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

```python
# mcp/session.py — keep as compat re-export
from ..db.session import scoped_session as mcp_request_session
__all__ = ["mcp_request_session"]
```

**Step 2: Verify + commit**

```bash
.venv/bin/python -m pytest tests/integration/mcp -q
grep -rn "@asynccontextmanager" src/transcription_api/mcp/session.py  # should be empty
git add src/transcription_api/db/session.py src/transcription_api/mcp/session.py
git commit -m "refactor(db): SPEC-capa4 G9 — move scoped_session ctx mgr to db/session.py"
```

### Task G9.2 — Tighten `bypass_scoping` in `api/upload.py`

**Files:**
- Modify: `src/transcription_api/api/upload.py`

**Step 1: Reduce bypass scope**

Replace the wide `with bypass_scoping(db):` block around the whole handler with two narrow blocks: one around the SELECT of upload_session, one around the INSERT/UPDATE pair. Add comment-fence:

```python
# api/upload.py top of file:
"""POST /api/upload (audio + image).

Auth model: this endpoint authenticates via the EPHEMERAL upload bearer
in `Authorization: Bearer <plaintext>`. The user identity is derived
from `upload_sessions.user_id` (a per-row column), NOT from the scoping
listener — db.info["user_id"] is intentionally NEVER armed in this
handler. Any new ORM query in this file MUST verify ownership manually
(via the upload_sessions row) and run inside `bypass_scoping(db)`.
"""
```

**Step 2: Commit**

```bash
git add src/transcription_api/api/upload.py
git commit -m "refactor(api): SPEC-capa4 G9 — tighten bypass_scoping scope + comment-fence"
```

---

## G10 — API error-shape + URL-build hygiene

**Severity**: LOW (lote).

### Task G10.1 — Extract `api/errors.py`

**Files:**
- Create: `src/transcription_api/api/errors.py`
- Modify: `src/transcription_api/api/upload.py`, `api/transcriptions.py` (use shared)

**Step 1: Move `_error_resp`**

```python
# api/errors.py
from fastapi.responses import JSONResponse

def error_response(status: int, code: str, reason: str, **extra) -> JSONResponse:
    body = {"error_code": code, "reason": reason, **extra}
    return JSONResponse(status_code=status, content={"detail": body})
```

**Step 2: Replace inline _error_resp / similar in API modules. Commit**:

```bash
git add src/transcription_api/api/errors.py src/transcription_api/api/upload.py src/transcription_api/api/transcriptions.py
git commit -m "refactor(api): SPEC-capa4 G10 — shared error_response helper"
```

### Task G10.2 — `urljoin` + `urlencode` in upload tool

**Files:**
- Modify: `src/transcription_api/mcp/tools/upload.py:185`

**Step 1: Patch URL build**

```python
from urllib.parse import urlencode, urljoin

upload_url = urljoin(settings.public_base_url + "/", path) + "?" + urlencode({"session": nonce})
```

**Step 2: Commit**

```bash
git add src/transcription_api/mcp/tools/upload.py
git commit -m "refactor(mcp): SPEC-capa4 G10 — upload_url uses urljoin + urlencode"
```

### Task G10.3 — Magic numbers to config

**Files:**
- Modify: `src/transcription_api/config.py` (add `upload_chunk_bytes: int = 65536`, `upload_size_margin: float = 1.05`, `upload_raw_filename: str = "original.bin"`)
- Modify: `src/transcription_api/api/upload.py` (use config values)

**Step 1: Patch**

```python
# config.py
upload_chunk_bytes: int = 65536       # 64 KiB streaming read
upload_size_margin: float = 1.05      # 5% margin over expected_size_bytes
upload_raw_filename: str = "original.bin"
```

```python
# api/upload.py
chunk = await file.read(settings.upload_chunk_bytes)
max_bytes = int(row.expected_size_bytes * settings.upload_size_margin)
target = upload_dir / settings.upload_raw_filename
```

**Step 2: Commit**

```bash
git add src/transcription_api/config.py src/transcription_api/api/upload.py
git commit -m "refactor(config): SPEC-capa4 G10 — extract upload magic numbers to settings"
```

---

# Tier 3 — Test hardening + governance

## G11 — AC coverage gaps

**Severity**: HIGH (test gaps). Depends on G1, G2, G6.

### Task G11.1 — RED+GREEN: AC-13 `/auth/me.mcp_url` test

**Files:**
- Modify: `tests/integration/auth/test_me_endpoint.py` (existing file, append test)

**Step 1: Test**

```python
@pytest.mark.asyncio
async def test_auth_me_returns_mcp_url_consistent_with_settings(authenticated_client, settings):
    resp = await authenticated_client.get("/auth/me")
    body = resp.json()
    assert body["mcp_url"] == f"{settings.public_base_url}/mcp"
```

**Step 2: Commit**

```bash
.venv/bin/python -m pytest tests/integration/auth/test_me_endpoint.py -v
git add tests/integration/auth/test_me_endpoint.py
git commit -m "test(auth): SPEC-capa4 G11 — AC-13 mcp_url assertion"
```

### Task G11.2 — RED+GREEN: AC-12 list-tools + list-resources counts

**Files:**
- Modify: `tests/integration/mcp/test_mcp_mount.py`

**Step 1: Test**

```python
@pytest.mark.asyncio
async def test_list_tools_returns_seven_canonical_names(mcp_client_with_bearer, active_bearer_plaintext):
    client = mcp_client_with_bearer(active_bearer_plaintext)
    tools = await client.list_tools()
    names = sorted(t.name for t in tools.tools)
    assert names == sorted([
        "request_upload_url", "start_transcription", "list_my_transcriptions",
        "search_my_transcriptions", "get_transcription", "delete_transcription",
        "get_user_info",
    ])


@pytest.mark.asyncio
async def test_list_resources_returns_two_uri_templates(mcp_client_with_bearer, active_bearer_plaintext):
    client = mcp_client_with_bearer(active_bearer_plaintext)
    res = await client.list_resource_templates()
    templates = sorted(t.uriTemplate for t in res.resourceTemplates)
    assert "transcription://{transcription_id}" in templates
    assert any("/images/{image_id}" in t for t in templates)
```

**Step 2: Commit**

```bash
git add tests/integration/mcp/test_mcp_mount.py
git commit -m "test(mcp): SPEC-capa4 G11 — AC-12 list_tools + list_resources canonical counts"
```

### Task G11.3 — RED+GREEN: scoping listener fail-closed test

**Files:**
- Create: `tests/integration/mcp/test_session.py`

**Step 1: Test**

```python
@pytest.mark.asyncio
async def test_mcp_request_session_with_no_user_raises_scoping_not_armed(db_engine):
    """ADR-015: a per-user query under scoped_session(None) → ScopingNotArmedError."""
    from transcription_api.db.session import scoped_session
    from transcription_api.db.scoping import ScopingNotArmedError
    from transcription_api.db.models import Transcription
    from sqlalchemy import select

    async with scoped_session(None) as db:
        with pytest.raises(ScopingNotArmedError):
            await db.execute(select(Transcription))
```

**Step 2: Commit**

```bash
git add tests/integration/mcp/test_session.py
git commit -m "test(mcp): SPEC-capa4 G11 — scoped_session(None) fail-closes per ADR-015"
```

### Task G11.4 — RED+GREEN: `last_used_at` bump failure path

**Files:**
- Modify: `tests/integration/mcp/test_mcp_middleware.py`

**Step 1: Test**

```python
@pytest.mark.asyncio
async def test_mcp_valid_bearer_returns_ok_when_last_used_at_bump_fails(
    mcp_client_with_bearer, active_bearer_plaintext, monkeypatch,
):
    """AC-14 failure path: best-effort UPDATE failure must NOT reject the request."""
    from sqlalchemy import update
    monkeypatch.setattr(
        "transcription_api.mcp.middleware._bump_last_used_at",
        AsyncMock(side_effect=RuntimeError("DB hiccup")),
    )
    client = mcp_client_with_bearer(active_bearer_plaintext)
    resp = await client.call_tool("get_user_info", {})
    assert not resp.is_error  # request succeeded despite bump failure
```

**Step 2: Commit**

```bash
git add tests/integration/mcp/test_mcp_middleware.py
git commit -m "test(mcp): SPEC-capa4 G11 — AC-14 last_used_at bump failure tolerated"
```

### Task G11.5 — RED+GREEN: AC-9 concurrency real with asyncio.gather

**Files:**
- Modify: `tests/integration/mcp/test_start_transcription.py`

**Step 1: Test**

```python
@pytest.mark.asyncio
async def test_concurrent_start_transcription_serializes_via_lock(
    mcp_client_with_bearer, active_bearer_plaintext, two_uploaded_audio_sessions, monkeypatch,
):
    """AC-9: two concurrent calls; second times out with LOCK_BUSY."""
    import asyncio
    from unittest.mock import AsyncMock

    async def slow_orchestrate(**kwargs):
        await asyncio.sleep(2)
        return {"transcription_id": uuid4(), "metadata": {"cache_hit": False}}

    monkeypatch.setattr("transcription_api.mcp.tools.start.orchestrate", slow_orchestrate)
    monkeypatch.setattr("transcription_api.config.settings.lock_wait_seconds", 0.5)

    client = mcp_client_with_bearer(active_bearer_plaintext)
    a, b = two_uploaded_audio_sessions
    r1, r2 = await asyncio.gather(
        client.call_tool("start_transcription", {"upload_id": str(a.id)}),
        client.call_tool("start_transcription", {"upload_id": str(b.id)}),
        return_exceptions=False,
    )
    error_count = sum(1 for r in (r1, r2) if r.is_error)
    success_count = 2 - error_count
    assert success_count == 1 and error_count == 1
    err_resp = r1 if r1.is_error else r2
    assert err_resp.error.data["error_code"] == "LOCK_BUSY"
```

**Step 2: Commit**

```bash
git add tests/integration/mcp/test_start_transcription.py
git commit -m "test(mcp): SPEC-capa4 G11 — AC-9 concurrent start_transcription via gather"
```

### Task G11.6 — Strengthen `delete_idempotent` test

**Files:**
- Modify: `tests/integration/mcp/test_delete_transcription.py`

**Step 1: Patch**

```python
async def test_delete_idempotent_preserves_first_deleted_at(
    mcp_client_with_bearer, active_bearer_plaintext, my_transcription, db_session,
):
    client = mcp_client_with_bearer(active_bearer_plaintext)
    await client.call_tool("delete_transcription", {"transcription_id": str(my_transcription.id)})
    first_deleted_at = (await db_session.execute(
        text("SELECT deleted_at FROM transcriptions WHERE id = :tid"),
        {"tid": my_transcription.id},
    )).scalar_one()
    # Second call → 404, but row's deleted_at must NOT change
    resp = await client.call_tool("delete_transcription", {"transcription_id": str(my_transcription.id)})
    assert resp.is_error
    second_deleted_at = (await db_session.execute(
        text("SELECT deleted_at FROM transcriptions WHERE id = :tid"),
        {"tid": my_transcription.id},
    )).scalar_one()
    assert first_deleted_at == second_deleted_at
```

**Step 2: Commit**

```bash
git add tests/integration/mcp/test_delete_transcription.py
git commit -m "test(mcp): SPEC-capa4 G11 — delete idempotent preserves deleted_at"
```

### Task G11.7 — Conftest helpers extraction

**Files:**
- Modify: `tests/integration/conftest.py`

**Step 1: Extract `_is_tool_error`, `_arm_context`, `_seed_user_with_bearer` as fixtures/utilities**

```python
@pytest.fixture
def assert_tool_error():
    def _impl(resp, expected_code: str):
        assert resp.is_error
        assert resp.error.data["error_code"] == expected_code
    return _impl
```

**Step 2: Replace ad-hoc usages across test files. Commit**:

```bash
git add tests/integration/conftest.py tests/integration/mcp/
git commit -m "refactor(tests): SPEC-capa4 G11 — extract conftest helpers"
```

---

## G12 — Lazy `app` import → ContextVars

**Severity**: STRATEGIC promoted to "do now". Depends on G2 + G6.

### Task G12.1 — RED: ContextVars armed per request

**Files:**
- Create: `tests/integration/mcp/test_runtime_context.py`

**Step 1: Test**

```python
@pytest.mark.asyncio
async def test_runtime_contextvars_armed_after_middleware(mcp_client_with_bearer, active_bearer_plaintext):
    """After bearer auth, ContextVars `_current_whisper_model` and `_current_pyannote_pipeline`
    are set via the middleware (no fallback to lazy app import)."""
    # call any tool that exercises the runtime context
    client = mcp_client_with_bearer(active_bearer_plaintext)
    resp = await client.call_tool("get_user_info", {})
    assert not resp.is_error
    # introspect: would require exposing a peek tool; instead, assert via test-only
    # path that start_transcription does NOT do `from ..main import app`
    import transcription_api.mcp.tools.start as start_mod
    src = open(start_mod.__file__).read()
    assert "from ..main import app" not in src
    assert "from ..main import" not in src
```

**Step 2: Commit RED**

```bash
git add tests/integration/mcp/test_runtime_context.py
git commit -m "test(mcp): SPEC-capa4 G12 — RED test no lazy app import in tools/start.py"
```

### Task G12.2 — GREEN: middleware arms runtime ContextVars

**Files:**
- Create: `src/transcription_api/mcp/runtime.py` (definitions)
- Modify: `src/transcription_api/mcp/middleware.py` (arming)
- Modify: `src/transcription_api/mcp/tools/start.py` (read ContextVars)

**Step 1: Define ContextVars**

```python
# src/transcription_api/mcp/runtime.py
from contextvars import ContextVar
from typing import Any

_current_whisper_model: ContextVar[Any] = ContextVar("_current_whisper_model", default=None)
_current_pyannote_pipeline: ContextVar[Any] = ContextVar("_current_pyannote_pipeline", default=None)
_current_models_status: ContextVar[Any] = ContextVar("_current_models_status", default=None)


def arm_runtime_from_state(app_state) -> None:
    _current_whisper_model.set(getattr(app_state, "whisper_model", None))
    _current_pyannote_pipeline.set(getattr(app_state, "pyannote_pipeline", None))
    _current_models_status.set({
        "whisper": getattr(app_state, "whisper_status", "unknown"),
        "pyannote": getattr(app_state, "pyannote_status", "unknown"),
        "whisper_detail": getattr(app_state, "whisper_detail", None),
        "pyannote_detail": getattr(app_state, "pyannote_detail", None),
    })


def get_runtime_models():
    return _current_whisper_model.get(), _current_pyannote_pipeline.get()


def get_runtime_status():
    return _current_models_status.get()
```

**Step 2: Wire middleware**

```python
# mcp/middleware.py — after _current_user_id is armed
from .runtime import arm_runtime_from_state

# inside dispatch, after bearer validation, before yielding to handler:
arm_runtime_from_state(request.app.state)
```

**Step 3: Use in tool**

```python
# mcp/tools/start.py — replace _get_app_state() and lazy app import
from ..runtime import get_runtime_models, get_runtime_status

# inside start_transcription:
whisper, pyannote = get_runtime_models()
status = get_runtime_status()
```

**Step 4: Commit**

```bash
.venv/bin/python -m pytest tests/integration/mcp -q
git add src/transcription_api/mcp/runtime.py src/transcription_api/mcp/middleware.py src/transcription_api/mcp/tools/start.py
git commit -m "refactor(mcp): SPEC-capa4 G12 — runtime ContextVars replace lazy app import"
```

---

## G13 — Wiki sync (Governance)

**Severity**: MEDIUM (no code blocker). Independent.

### Task G13.1 — `wiki/05_modelo_datos.md` §8 add error codes

**Files:**
- Modify: `wiki/05_modelo_datos.md` §8 error taxonomy

**Step 1: Add rows**

```markdown
| `MODELS_NOT_LOADED` | 503 | RF-MCP-02 / RF-TRX | Whisper o pyannote no están en estado `ready` (lifespan startup pendiente o falló). |
| `PIPELINE_TIMEOUT` | 504 | RF-MCP-02 / RF-TRX | Pipeline excedió `pipeline_timeout_seconds`. |
```

**Step 2: Commit**

```bash
git add wiki/05_modelo_datos.md
git commit -m "docs(wiki): SPEC-capa4 G13 — add MODELS_NOT_LOADED + PIPELINE_TIMEOUT to error taxonomy"
```

### Task G13.2 — `wiki/02_arquitectura.md` §3 + §5 mount + middleware

**Files:**
- Modify: `wiki/02_arquitectura.md`

**Step 1: Patch** §3 component list to add `MCP server (FastMCP, Streamable HTTP, mounted at /mcp)` and §5 to document `BearerAuthMiddleware`, `_current_user_id` ContextVar bridge, `scoped_session(user_id)` ctx mgr.

**Step 2: Commit**

```bash
git add wiki/02_arquitectura.md
git commit -m "docs(wiki): SPEC-capa4 G13 — document MCP mount + middleware in §3 + §5"
```

### Task G13.3 — Create `wiki/ADR/ADR-016.md`

**Files:**
- Create: `wiki/ADR/ADR-016.md` "Layered scoping defense: listener fail-closed + startup classification guard"

**Step 1: Write ADR**

```markdown
# ADR-016: Defensa en capas para per-user scoping

## Estado
Aceptada — 2026-05-07
Hereda contexto de [ADR-015](ADR-015.md). NO la reemplaza.

## Contexto
ADR-015 estableció el listener `do_orm_execute` fail-closed contra modelos
con `user_id`. Capa 4 review identificó un fail-OPEN trap: un modelo per-user
futuro que omita la columna `user_id` no es enrolado por `_scoped_models()`
y queries cross-user devuelven todas las filas silenciosamente.

## Decisión
Defensa en dos capas:
1. **Runtime listener** (ADR-015 vigente): inyecta WHERE user_id=X en cada query
   contra modelos con `user_id`.
2. **Startup classification guard** (Capa 4): `_validate_model_classification()`
   itera `Base.registry.mappers` y assert que cada modelo está en `user_id` set
   o en `_NON_SCOPED_MODELS = frozenset({"User"})`. Service refuse to start si
   un modelo nuevo no encaja.

## Consecuencias
- Cualquier modelo nuevo en Capa 5+ que olvide `user_id` y no esté allowlisted
  ROMPE startup con `ScopingClassificationError`. Loud failure, no silent leak.
- Tests existentes siguen verde (current state classifies cleanly).
- Para agregar un modelo global (Config, AuditLog), update `_NON_SCOPED_MODELS`
  con review explícito de Privacy implications.

## Referencias
- Implementación: `src/transcription_api/db/scoping.py::_validate_model_classification`.
- Tests: `tests/unit/db/test_scoping_classification.py`.
- Spec §10 entry de Capa 4.
```

**Step 2: Update `wiki/02_arquitectura.md` §7 ADR index**

```markdown
| [ADR-016](ADR/ADR-016.md) | Defensa en capas para per-user scoping | Aceptada | 2026-05-07 | §8 — startup guard complementa listener fail-closed |
```

**Step 3: Commit**

```bash
git add wiki/ADR/ADR-016.md wiki/02_arquitectura.md
git commit -m "docs(wiki): SPEC-capa4 G13 — ADR-016 layered scoping defense"
```

### Task G13.4 — `wiki/RF/RF-MCP.md` RF-MCP-00 + RF-MCP-02 step 6

**Files:**
- Modify: `wiki/RF/RF-MCP.md`

**Step 1: Add canonical scoping paragraph in RF-MCP-00 §Per-user scoping**

```markdown
> **Implementation note**: tool/resource handlers operating on per-user models
> issue ORM queries WITHOUT explicit `user_id` predicate. The listener
> (ADR-015) AND-injects `WHERE user_id = X` from `db.info["user_id"]` armed by
> the bearer middleware. ADR-016 adds a startup guard ensuring no per-user
> model lacks `user_id` (defensa en capas).
```

**Step 2: Add grace to RF-MCP-02 step 6**

```markdown
| 6 | Si `expires_at + UPLOAD_SESSION_GRACE_SECONDS < now`: `UPLOAD_SESSION_NOT_FOUND`. Default grace = 30s para tolerar clock skew clock cliente/servidor. |
```

**Step 3: Commit**

```bash
git add wiki/RF/RF-MCP.md
git commit -m "docs(wiki): SPEC-capa4 G13 — RF-MCP scoping note + step 6 grace"
```

### Task G13.5 — Drift log entry D-049

**Files:**
- Modify: `docs/sesiones/2026-05-05-wiki-drifts.md`

**Step 1: Append**

```markdown
### D-049 🟢 Capa 4 review-fixes G13: ADR-016 documenta defensa en capas para scoping

**Asumido (ADR-015)**: listener fail-closed era suficiente para enforce per-user scoping.

**Reality (Capa 4 review-fixes G13)**: listener depende de columna `user_id`. Modelo
futuro sin esa columna es silent fail-open. ADR-016 agrega startup guard.

**Resolución**: nuevo ADR-016 (no mutar ADR-015). Implementación en commit `c5d5115`.

**Lección**: defensa en capas — runtime + startup. Same Privacy invariant, dos checkpoints.
```

**Step 2: Commit**

```bash
git add docs/sesiones/2026-05-05-wiki-drifts.md
git commit -m "docs(drifts): SPEC-capa4 G13 — D-049 ADR-016 layered scoping defense"
```

---

## G14 — CI runner para `requires_docker`

**Severity**: HIGH (operational). Independent.

### Task G14.1 — `.github/workflows/test.yml`

**Files:**
- Create: `.github/workflows/test.yml`

**Step 1: Workflow**

```yaml
name: Test
on:
  pull_request:
  push:
    branches: [master]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: transcription
          POSTGRES_PASSWORD: transcription
          POSTGRES_DB: transcription
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -e ".[dev]"
      - run: ruff check src/ tests/
      - run: pytest tests/ -m "not e2e and not requires_gpu and not requires_ffmpeg" -v
        env:
          POSTGRES_HOST: localhost
          POSTGRES_PORT: "5432"
          POSTGRES_USER: transcription
          POSTGRES_PASSWORD: transcription
          POSTGRES_DB: transcription
```

**Step 2: Commit**

```bash
git add .github/workflows/test.yml
git commit -m "ci: SPEC-capa4 G14 — GH Actions workflow with postgres service"
```

### Task G14.2 — `AGENTS.md` test section

**Files:**
- Modify: `AGENTS.md`

**Step 1: Patch the testing section** to document markers + the GH Actions workflow + how to run individual marker subsets locally.

**Step 2: Commit**

```bash
git add AGENTS.md
git commit -m "docs(agents): SPEC-capa4 G14 — document test markers + CI workflow"
```

---

# Final consolidation

## Pre-merge checklist

After all 14 groups land:

```bash
# 1. Suite green end-to-end (with Docker daemon for requires_docker tests)
.venv/bin/python -m pytest tests/ -m "not e2e and not requires_gpu and not requires_ffmpeg" -v

# 2. Lint clean
.venv/bin/python -m ruff check src/ tests/

# 3. Smoke checklist completed on rig (docs/sesiones/2026-05-06-capa4-rig-smoke.md, every AC OK)

# 4. Drift log updated (D-049 added)

# 5. Wiki synced — verify
grep -E "MODELS_NOT_LOADED|PIPELINE_TIMEOUT" wiki/05_modelo_datos.md
grep -E "ADR-016" wiki/02_arquitectura.md
ls wiki/ADR/ADR-016.md

# 6. AC traceability updated — 16/16 ACs marked YES with commit hashes
grep -c "^| AC-" docs/sesiones/2026-05-06-capa4-mcp-plan.md  # should be 16+

# 7. Squash commit (final merge to master)
git log master..HEAD --oneline | wc -l   # ~70 commits expected post-fix
git checkout master
git merge --squash feat/capa4-mcp
git commit -m "feat(capa4): MCP server + chunked upload + review-fixes (SPEC-capa4)"
```

## Squash commit message template

```
feat(capa4): MCP server (Streamable HTTP) + chunked upload + 14 review-fix groups

Tools MCP (RF-MCP-01..10): request_upload_url, start_transcription,
list_my_transcriptions, search_my_transcriptions, get_transcription,
delete_transcription, get_user_info. Resources transcription://<id> +
transcription://<id>/images/<image_id> (RF-MCP-07/08). Auth middleware
RF-MCP-11 con bearer SHA-256 hash compare + last_used_at best-effort.

REST endpoints `POST /api/upload` (audio) y `POST /api/upload-image`
(con magic bytes validation) + bearer ephemeral validation contra
upload_bearer_hash (D-044-impl).

Reuse de pipeline.orchestrator.orchestrate (Capa 3) sin tocar el primitive
del lock. ADR-015 fail-closed scoping reusado vía scoped_session(user_id)
context manager. ADR-016 nueva — defensa en capas con startup guard
contra modelos no clasificados.

Legacy POST /api/transcriptions marcado deprecated=True (removal en Capa 5).

Refs: SPEC-capa4-mcp-v1, ADR-011 (MCP-first), ADR-013 (uploads bearer),
ADR-015 (scoping listener), ADR-016 (layered defense), drifts D-026, D-027,
D-028, D-040, D-042, D-043, D-044, D-045, D-046, D-047, D-048, D-049.
```

---

## Execution Handoff

Plan completo y guardado en `docs/sesiones/2026-05-07-capa4-review-fixes-tdd-plan.md`. Two execution options:

**1. Subagent-Driven (this session)** — I dispatch fresh subagent per task, review between tasks, fast iteration.

**2. Parallel Session (separate)** — Open new session with `superpowers:executing-plans`, batch execution with checkpoints.

**Which approach?**
