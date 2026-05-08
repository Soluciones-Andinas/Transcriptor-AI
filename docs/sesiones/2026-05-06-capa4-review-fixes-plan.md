# Capa 4 — Review Fixes Plan

**Branch**: `feat/capa4-mcp` (commits `0071c77..7a063f4`)
**Generated**: 2026-05-07
**Inputs**: 5 review agents (silent-failure-hunter, pr-test-analyzer, code-reviewer, sandinas-code-reviewer, code-architect) on the Capa 4 diff.

## 0. Resumen agregado

| Lente | CRIT | HIGH | MED | LOW | STRAT |
|---|---|---|---|---|---|
| silent-failure-hunter | 0 | 1 | 4 | 3 | 2 |
| pr-test-analyzer | 3 | 7 | 6 | 2 | 3 |
| code-reviewer | 0 | 4 | 11 | 5 | 2 |
| sandinas-code-reviewer | 0 | 6 | 6 | 2 | 2 |
| code-architect | 0 | 2 | 4 | 2 | 2 |
| **Total bruto** | **3** | **20** | **31** | **14** | **11** |

Tras dedup cross-agent: **3 CRITICAL**, **~14 HIGH** únicos, **~22 MEDIUM** únicos.

### 0.1 Bug confirmado por 3 lentes independientes (top priority)

**`POST /api/upload-image` no existe en producción**.
- `mcp/tools/upload.py:184` emite `upload_url=f"{public_base_url}/api/upload-image?session={nonce}"` para `kind=image`.
- `api/upload.py` solo expone `/api/upload` (audio).
- Cualquier flujo image se rompe en 404 silencioso de FastAPI router.
- AC-7 no puede pasar end-to-end.
- pr-test-analyzer CRITICAL #2 + code-architect HIGH #2 + sandinas-code-reviewer HIGH #1.

### 0.2 Tensiones entre agentes resueltas

| Tensión | Resolución |
|---|---|
| Lazy `from ..main import app` (agent3 fix-now vs agent5 defer) | **Defer**. Aplicar opción ContextVars de agent5 (extender patrón existente `_current_user_id`). En G12, Tier 2. |
| `list/search` clamp: raise (agent1) vs helper (agent3) | **Ambos**. Raise `INVALID_PARAMETER` para `limit<1`, `offset<0`, `len(query)<1`; clamp silente entre `[MAX, MAX*2]`; raise para `>MAX*2` con `extra={"max_limit":MAX}`. En G5. |
| `last_used_at` best-effort (agent1 LOW vs agent2 MEDIUM) | **No contradicen**. Behaviour legítimo (RF-MCP-00 §Side effects); falta test del failure branch. En G11. |
| `bypass_scoping` widening (agent1 STRAT vs agent3 MEDIUM) | **Tighten scope** + comment-fence al top de `upload_audio`. En G9. |

---

## 1. Cross-confirmation matrix (findings detectados por múltiples agentes)

| Concern | Agentes | Severidad merged |
|---|---|---|
| `/api/upload-image` missing | pr-test, architect, sandinas | **CRITICAL** |
| `start_transcription` 155 LOC + duplicates `_models_loaded_or_503` | silent-failure (HIGH), code-reviewer (HIGH x2), pr-test (HIGH kwargs) | **HIGH** |
| `tools/transcription.py` 589 LOC, 6 tools | code-reviewer (HIGH), code-architect (MEDIUM) | **HIGH** |
| `list/search` clamp duplication + silent | silent-failure (MEDIUM), code-reviewer (HIGH), pr-test (MEDIUM tautological) | **HIGH** |
| `lookup_or_not_found` helper (privacy invariant) | silent-failure (STRAT), code-reviewer (STRAT), pr-test (STRAT) | **STRAT → promote to HIGH** |
| `start_transcription` cleanup no en `finally` | silent-failure (MEDIUM), sandinas (HIGH cita CLAUDE.md §11) | **HIGH** |
| Bearer hash + parser duplication | code-reviewer (MEDIUM x2), sandinas (no hash helper) | **MEDIUM** |
| `serialize_*` should be public module | code-reviewer (STRAT), code-architect (MEDIUM) | **MEDIUM** |
| `mcp_request_session` location | code-architect (HIGH) — NEW | **HIGH** |
| Wiki sync (errors §8, arch §3, ADR-016) | sandinas-only (HIGH x4) | **HIGH** governance |

---

## 2. Grupos de fix (ordenados por prioridad de merge)

### Tier 1 — Blocking (must land before merge to master)

#### G1 · Image upload pipeline missing
**Severidad**: CRITICAL
**Files**:
- `src/transcription_api/api/upload.py` — agregar `POST /api/upload-image`
- `src/transcription_api/main.py:62-64` — agregar `settings.blobs_dir.mkdir(parents=True, exist_ok=True)`
- `src/transcription_api/mcp/tools/upload.py:96-184` — validación path actual del image branch (revisar)
- `tests/integration/api/test_upload_image.py` — nuevo
- `tests/integration/mcp/test_image_e2e.py` — wire test request_upload_url(image) → POST → resource fetch

**Findings que cierra**:
- pr-test-analyzer CRITICAL #2 (AC-7 image upload chain).
- code-architect HIGH #2 (dangling MCP contract).
- sandinas-code-reviewer HIGH #1 (RF-IMG-02 not implemented) + MEDIUM (lifespan no mkdir).

**Plan**:
1. Implement `POST /api/upload-image` en `api/upload.py` siguiendo RF-IMG-02:
   - Bearer hash compare (igual que `/api/upload`).
   - Validar magic bytes (imghdr / Pillow header peek) contra `expected_mime_type`.
   - INSERT en `images` con `transcription_id`, `user_id`, `mime_type`, `original_filename`, `file_path`.
   - Move bytes a `<DATA_DIR>/blobs/<user_id>/<transcription_id>/<image_id>.<ext>`.
   - Marcar `upload_sessions.status='uploaded'`.
2. Lifespan: `settings.blobs_dir.mkdir(parents=True, exist_ok=True)` después de línea 64.
3. Tests:
   - `test_upload_image_happy` → row en `images`, bytes en `<DATA_DIR>/blobs/<user_id>/<tid>/<iid>.<ext>`, status flip.
   - `test_upload_image_wrong_magic_bytes_returns_400`.
   - `test_upload_image_cross_user_returns_404` (bearer de otro user con session válida).
   - `test_image_e2e` wire: request_upload_url → POST → resource fetch returns same bytes.

**Test assertion post-fix**: `pytest tests/integration/api/test_upload_image.py tests/integration/mcp/test_image_e2e.py -v` PASS; AC-7 marcado como YES en traceability matrix del plan.

**Effort**: 4-6h (endpoint + magic-bytes validation + 4 tests).

---

#### G2 · `start_transcription` extract + cleanup en finally
**Severidad**: HIGH (consolida 4 findings)
**Files**:
- `src/transcription_api/mcp/tools/transcription.py:125-279`
- `src/transcription_api/api/transcriptions.py:_models_loaded_or_503` (extracción)
- `src/transcription_api/main.py` (helper compartido) o nuevo `src/transcription_api/runtime/readiness.py`
- `tests/integration/mcp/test_start_transcription.py`

**Findings que cierra**:
- silent-failure HIGH (`MODELS_NOT_LOADED` masking).
- code-reviewer HIGH (155 LOC, gate duplicado).
- silent-failure MEDIUM (cleanup fuera de try/finally → orphan dir).
- sandinas-code-reviewer HIGH (CLAUDE.md §11 violation).
- pr-test HIGH (no asserta orchestrate kwargs).

**Plan**:
1. Extraer `_models_ready_or_raise(app_state) -> None` en módulo compartido (mover de `api/transcriptions.py::_models_loaded_or_503`). Que ambas rutas (REST + MCP) lo invoquen. Si `hasattr(app_state, "whisper_status")` es False, raise `RuntimeError` (`error_id=LIFESPAN_DID_NOT_ARM_STATE`) — no enmascarar con `"loading"`.
2. Split `start_transcription` en helpers privados:
   - `_load_upload_row(db, upload_id, user_id)` — incluye bypass classification: aplica RF-MCP-02 step 6 (con grace).
   - `_consume_upload(db, row)` — flip `status='consumed'` + commit boundary.
   - `_cleanup_upload_dir(upload_dir)` — `shutil.rmtree(..., ignore_errors=True)`.
3. **Cleanup en `try/finally`** alrededor de `orchestrate(...) + UPDATE`:
   ```python
   try:
       result = await orchestrate(...)
       await _consume_upload(db, row)
   finally:
       _cleanup_upload_dir(upload_dir_for_session)
   ```
4. Test: agregar assertion sobre `orchestrate.call_args.kwargs`: `user_id`, `language`, `file_path`, `cache_store is not None`, `min_speakers`, `max_speakers`, `whisper_model`, `pyannote_pipeline`. Variantes con `language="en"` y `max_speakers=4`.
5. Test: añadir `test_start_transcription_cleans_upload_dir_on_orchestrate_failure` (mock `orchestrate` con `side_effect=RuntimeError`, asertar `not upload_dir_for_session.exists()`).

**Test assertion post-fix**: orphan directory test PASS, kwargs assertion PASS, `start_transcription` < 80 LOC, `_models_ready_or_raise` invocado por dos call sites.

**Effort**: 3-4h.

---

#### G3 · FTS regconfig binding (Performance)
**Severidad**: HIGH
**Files**:
- `src/transcription_api/mcp/tools/transcription.py:355-402` (`_FTS_CONFIG`, `plainto_tsquery`, `to_tsvector`, `ts_headline` invocations)
- `tests/integration/mcp/test_search_my_transcriptions.py` — agregar regression test

**Findings que cierra**:
- sandinas-code-reviewer HIGH #2 (FTS misses GIN index).

**Plan**:
1. Reemplazar todos los `func.to_tsvector(_FTS_CONFIG, ...)` y `func.plainto_tsquery(_FTS_CONFIG, ...)` y `func.ts_headline(_FTS_CONFIG, ...)` por:
   ```python
   from sqlalchemy import literal_column
   _FTS_REGCONFIG = literal_column("'spanish'::regconfig")
   # uso
   func.to_tsvector(_FTS_REGCONFIG, Transcription.text_content)
   ```
   Alternativa: `cast(literal('spanish'), REGCONFIG)` from `sqlalchemy.dialects.postgresql.REGCONFIG`.
2. Test regression: seed 1k filas, capturar `EXPLAIN (FORMAT JSON)` del query generado, asertar plan contains `Bitmap Index Scan on idx_transcriptions_text_fts`. Skip si no hay testcontainer (fixture ya existe).
3. Move `_FTS_CONFIG = "spanish"` al `config.py` como `settings.fts_config: str = "spanish"`.

**Test assertion post-fix**: regression test PASS bajo `requires_docker`.

**Effort**: 2h.

---

#### G4 · `lookup_or_not_found` helper (privacy invariant)
**Severidad**: HIGH (promoted from STRATEGIC — 3 agentes convergieron)
**Files**:
- `src/transcription_api/mcp/lookup.py` (nuevo)
- `src/transcription_api/mcp/tools/transcription.py` (5 sites: get_transcription, delete_transcription, start_transcription upload row)
- `src/transcription_api/mcp/tools/upload.py` (1 site: kind=image transcription_id check)
- `src/transcription_api/mcp/resources.py` (1 site: image_resource)
- `tests/unit/mcp/test_lookup_helper.py` (nuevo)

**Findings que cierra**:
- silent-failure STRATEGIC (5 sites collapse pattern).
- code-reviewer STRATEGIC (lookup pattern).
- pr-test STRATEGIC (privacy invariant enforced by copy-paste).

**Plan**:
```python
# src/transcription_api/mcp/lookup.py
from typing import TypeVar, Type
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .errors import raise_tool_error

T = TypeVar("T")

async def lookup_owned_or_404(
    db: AsyncSession,
    model: Type[T],
    id_value,
    *,
    error_code: str,
    error_message: str,
    soft_delete: bool = True,
) -> T:
    """SELECT a per-user row through the scoping listener. Collapse all
    causes of "no row" (cross-user, unknown, soft-deleted) into the same
    NOT_FOUND error per ADR-015. Caller must run inside mcp_request_session.
    """
    stmt = select(model).where(model.id == id_value)
    if soft_delete and hasattr(model, "deleted_at"):
        stmt = stmt.where(model.deleted_at.is_(None))
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise_tool_error(error_code, error_message, 404)
    return row  # type: ignore[return-value]  # raise_tool_error is NoReturn (see G8)
```
Refactor 5 call sites to use the helper.

Unit test: `test_lookup_owned_or_404_collapses_cross_user_unknown_soft_deleted` — seed 3 rows (own active, own soft-deleted, other-user active); each combination → 404 (except own active → row).

**Test assertion post-fix**: 5 sites use the helper; helper unit test PASS; existing cross-user/soft-deleted tests still PASS.

**Effort**: 3h (helper + 5 refactors + unit test).

---

#### G5 · `list/search` params hardening
**Severidad**: HIGH
**Files**:
- `src/transcription_api/mcp/tools/transcription.py:316-321, 394-397` (clamp)
- `src/transcription_api/mcp/_clamp.py` (nuevo, opcional)
- `tests/integration/mcp/test_search_my_transcriptions.py` (clamp regression)
- `tests/integration/mcp/test_list_my_transcriptions.py` (raise para input claramente inválido)

**Findings que cierra**:
- silent-failure MEDIUM (silent clamp).
- code-reviewer HIGH (duplication + anti-idiom).
- pr-test MEDIUM (clamp test tautological).

**Plan**:
1. Helper:
   ```python
   def _clamp(value: int, lo: int, hi: int) -> int:
       return max(lo, min(value, hi))
   ```
2. Política de validación (resolución de tensión):
   - `limit < 1` o `offset < 0` o `len(query) < 1` → `raise_tool_error("INVALID_PARAMETER", ..., extra={"min": 1})` (claramente bug del cliente).
   - `MAX < limit <= MAX*2` → clamp silente a MAX (tolerancia a cliente bien-intencionado).
   - `limit > MAX*2` → `raise_tool_error("INVALID_PARAMETER", ..., extra={"max_limit": MAX})`.
3. Test: seed 60 transcriptions matching `"arquitectura"`, llamar `search_my_transcriptions(query="arquitectura", limit=999)` → expect raise. `limit=80` (entre MAX y MAX*2) → 50 results.
4. Test: `search_my_transcriptions(query="arquitectura", limit=-1)` → raise INVALID_PARAMETER.
5. Test: `list_my_transcriptions(limit=200)` → raise (límite duro >100*2=200... reformular, usar 250).

**Test assertion post-fix**: 4 tests nuevos PASS, existentes siguen PASS, helper en `_clamp.py` con 2-line body.

**Effort**: 2h.

---

### Tier 2 — High value, no-block (target same PR pero opcional postpone)

#### G6 · Tools file split + serializers module
**Severidad**: HIGH
**Files**:
- `src/transcription_api/mcp/serializers.py` (nuevo)
- Split `src/transcription_api/mcp/tools/transcription.py` → `tools/{start,list,search,get,delete,user}.py`
- `src/transcription_api/mcp/__init__.py` (imports)
- `src/transcription_api/mcp/resources.py` (use `serializers.serialize_full`)

**Findings que cierra**:
- code-reviewer HIGH (589 LOC, 6 tools).
- code-architect MEDIUM (cohesion drift, spec §8 layout).
- code-reviewer STRATEGIC + code-architect STRAT (serializers public).

**Plan**:
1. Crear `mcp/serializers.py`:
   ```python
   def serialize_summary(row) -> dict[str, Any]: ...
   def serialize_full(row) -> dict[str, Any]: ...
   def serialize_image(row) -> dict[str, Any]: ...
   def unwrap_segments(blob: Any) -> list: ...  # fix code-reviewer MEDIUM #11
   ```
2. Split `tools/transcription.py` por verbo. Cada archivo ≤200 LOC.
3. Actualizar imports en `mcp/__init__.py` y `mcp/resources.py`.
4. Smoke test: existing test suite runs unchanged (no behavior changes).

**Test assertion post-fix**: pytest sin cambios en counts; ningún archivo en `mcp/tools/` >300 LOC.

**Effort**: 2h (mecánico).

---

#### G7 · Bearer hash + parser consolidation
**Severidad**: MEDIUM
**Files**:
- `src/transcription_api/auth/mcp_bearer.py` (export `hash_bearer`, opcional `compare_bearer_hash`)
- `src/transcription_api/auth/header.py` (nuevo, `parse_bearer`)
- 3 hash sites: `mcp/middleware.py:108`, `mcp/tools/upload.py:162-164`, `api/upload.py:125`
- 2 parser sites: `mcp/middleware.py:162-175`, `api/upload.py:73-77`

**Findings que cierra**:
- code-reviewer MEDIUM (parser duplication).
- sandinas-code-reviewer MEDIUM (spec §8 dijo agregar `compare_token_hash`).

**Plan**:
1. `auth/header.py::parse_bearer(header_value: str | None) -> str | None`.
2. Promote `hash_bearer` (ya existe) — replace 2 inline `sha256(plaintext.encode("ascii")).hexdigest()` en upload paths.
3. Replace 2 parser sites con `parse_bearer(authorization)`.

**Test assertion post-fix**: tests existentes PASS sin cambios; `grep -r "sha256(.*encode.*ascii" src/` returns only `auth/mcp_bearer.py` y opcionalmente la migración alembic.

**Effort**: 1h.

---

#### G8 · Type / correctness microfixes
**Severidad**: MEDIUM (stack de fixes pequeños)
**Files** (varios):
- `src/transcription_api/mcp/errors.py:27-51` — `raise_tool_error -> NoReturn`
- `src/transcription_api/db/scoping.py:124-126` — `_resolve_user_id` valida `isinstance(user_id, UUID)`, raise `ScopingNotArmedError` si no
- `src/transcription_api/mcp/serializers.py::unwrap_segments` (G6) — fix `else: []` que descarta lists válidas
- `src/transcription_api/mcp/tools/delete.py` (post-G6) — log cascade rowcount
- `config.py` — `upload_session_grace_seconds: int = 30` (RF-MCP-02 step 6)
- `src/transcription_api/mcp/tools/start.py` (post-G6) y `api/upload.py:106` — usar grace en check `expires_at + grace`

**Findings que cierra**:
- code-reviewer MEDIUM (`raise_tool_error` `-> NoReturn`).
- code-reviewer MEDIUM (`_resolve_user_id` type lies).
- code-reviewer MEDIUM (`_serialize_full` shape branch).
- code-reviewer MEDIUM (delete cascade no log).
- sandinas-code-reviewer HIGH #5 (`expires_at` sin grace).

**Plan**: cada fix es ≤5 LOC. Lote único.

**Test assertion post-fix**:
- pyright/mypy narrowing OK después de cada `raise_tool_error` call.
- `test_resolve_user_id_raises_on_non_uuid_string` (nuevo unit test).
- `test_unwrap_segments_handles_bare_list` (nuevo unit test).
- `test_upload_session_within_grace_succeeds` (boundary at `expires_at - 1s`, `expires_at + grace - 1s`, `expires_at + grace + 1s`).

**Effort**: 2h.

---

#### G9 · `mcp_request_session` relocation + bypass_scoping tightening
**Severidad**: MEDIUM
**Files**:
- `src/transcription_api/db/session.py` (mover el ctx mgr)
- `src/transcription_api/mcp/session.py` (re-export)
- `src/transcription_api/api/upload.py:88-181` — tighten `bypass_scoping` scope + comment-fence

**Findings que cierra**:
- code-architect HIGH #1 (location wrong).
- silent-failure STRATEGIC (`bypass_scoping` widening).
- code-reviewer MEDIUM (sync inside async with → single line).

**Plan**:
1. Move ctx mgr body de `mcp/session.py` a `db/session.py` como `scoped_session(user_id: UUID | None)`. (`None` → no arming → listener fail-closes en cualquier per-user query.)
2. `mcp/session.py` queda como `from ..db.session import scoped_session as mcp_request_session` (re-export para compat).
3. `api/upload.py`: tighten `bypass_scoping` a wrappers exactos del SELECT y del UPDATE; agregar comment-fence al top de `upload_audio` declarando que el endpoint NO arma `db.info["user_id"]` y cualquier nueva query ORM debe verificar ownership manualmente.

**Test assertion post-fix**: tests existentes PASS; `grep -r "from .session import" src/transcription_api/mcp/` returns 0 (todo via `db.session`).

**Effort**: 1.5h.

---

#### G10 · API error-shape + URL-build hygiene
**Severidad**: LOW (lote)
**Files**:
- `src/transcription_api/api/errors.py` (nuevo, mover `_error_resp`)
- `src/transcription_api/api/transcriptions.py` (use shared error helper)
- `src/transcription_api/mcp/tools/upload.py:185` — usar `urljoin` + `urlencode`
- `src/transcription_api/config.py` — `upload_chunk_bytes`, `upload_size_margin`, `upload_raw_filename`

**Findings que cierra**:
- code-reviewer LOW (inconsistent error builders).
- code-reviewer LOW (URL string-concat).
- code-reviewer MEDIUM (magic numbers).

**Plan**: refactor mecánico.

**Effort**: 1h.

---

### Tier 3 — Test hardening + governance (post-merge OK pero bloquea siguiente capa)

#### G11 · AC coverage gaps + cross-user fail-closed test
**Severidad**: HIGH (test suite gaps)
**Files**:
- `tests/integration/auth/test_me_endpoint.py` — agregar AC-13 mcp_url assertion
- `tests/integration/mcp/test_mcp_mount.py` — agregar `test_list_tools_returns_seven_canonical_names`, `test_list_resources_returns_two_uri_templates`
- `tests/integration/mcp/test_session.py` (nuevo) — `test_mcp_request_session_with_no_user_id_raises_scoping_not_armed`
- `tests/integration/mcp/test_get_transcription.py` — `test_get_transcription_duration_seconds_is_float`
- `tests/integration/mcp/test_delete_transcription.py` — strengthen `test_delete_idempotent_second_call_returns_not_found` (asertar `deleted_at` preservation)
- `tests/integration/mcp/test_mcp_middleware.py` — `test_mcp_valid_bearer_returns_ok_when_last_used_at_bump_fails`
- `tests/integration/mcp/test_request_upload_url.py` — `assert upload_url == f"{settings.public_base_url}/api/upload?session={row.nonce}"`
- `tests/integration/mcp/test_search_my_transcriptions.py` — replace tautological clamp test (G5 lo cierra)
- `tests/integration/conftest.py` — extraer `_is_tool_error`, `_arm_context`, `_seed_user_with_bearer` como fixtures

**Findings que cierra**:
- pr-test CRITICAL #1 (AC-13 mcp_url no testeado).
- pr-test CRITICAL #3 (AC-12 list-tools).
- pr-test HIGH #4 (orchestrate kwargs — closed by G2).
- pr-test HIGH #5 (AC-9 concurrency — agregar test concurrencia real con asyncio.gather).
- pr-test HIGH #6 (cross-user no triggers ScopingNotArmedError).
- pr-test HIGH #8 (deleted_at preservation).
- pr-test HIGH #9 (duration_seconds float).
- pr-test MEDIUM #14 (AC-14 failure path).
- pr-test LOW #16 (helper duplicated).

**Plan**: ~9 tests nuevos + 3 tests strengthened + helper extraction.

**Test assertion post-fix**: AC traceability matrix de `2026-05-06-capa4-mcp-plan.md` actualizado: 16/16 ACs con coverage real (no PARTIAL).

**Effort**: 5-6h.

---

#### G12 · Lazy `app` import → ContextVars (defer-or-do)
**Severidad**: STRATEGIC (decisión: hacer ahora, opción agent5 #1)
**Files**:
- `src/transcription_api/mcp/middleware.py` — armar `_current_whisper`, `_current_pyannote`, `_current_models_status` ContextVars desde `app.state` por request
- `src/transcription_api/mcp/runtime.py` (nuevo) — definición de los ContextVars
- `src/transcription_api/mcp/tools/start.py` (post-G6) — leer ContextVars en lugar de `from ..main import app`

**Findings que cierra**:
- code-reviewer HIGH (lazy import asymmetric).
- code-architect STRATEGIC (couples to FastAPI singleton).

**Plan**: ~30 LOC en middleware. Tests `monkeypatch.setattr(app.state, ...)` siguen funcionando porque middleware lee de `app.state` y arma ContextVars. Tests directos al tool ahora arman ContextVars vía helper en lugar de `app.state`.

**Decisión**: hacer en este review-fix porque G2 ya toca `start_transcription` y aprovecha el split. Si bloquea, defer y dejar el TODO en código.

**Effort**: 2-3h.

---

#### G13 · Wiki sync (Governance)
**Severidad**: MEDIUM (governance, no bloquea código)
**Files**:
- `wiki/05_modelo_datos.md` §8 — agregar `MODELS_NOT_LOADED` (503), `PIPELINE_TIMEOUT` (504)
- `wiki/02_arquitectura.md` §3 + §5 — documentar `app.mount("/mcp", mcp_app)` + `BearerAuthMiddleware` + `_current_user_id` ContextVar bridge
- `wiki/ADR/ADR-016.md` (nuevo) — "Layered scoping defense: listener fail-closed + startup classification guard". Hereda contexto de ADR-015. NO mutar ADR-015.
- `wiki/RF/RF-MCP.md` RF-MCP-00 §Per-user scoping — agregar párrafo canónico: "queries omit explicit user_id predicate; listener AND-injects per ADR-015".
- `wiki/RF/RF-MCP.md` RF-MCP-02 step 6 — decidir grace (G8 lo implementa con default 30s).
- `docs/sesiones/2026-05-05-wiki-drifts.md` — entry D-049 si se decide no mover ADR-015.

**Findings que cierra**:
- sandinas-code-reviewer HIGH #4, #6, #7 (4 wiki gaps).
- sandinas-code-reviewer MEDIUM (RF wording divergencia 3 sitios).

**Plan**: edición de wiki, sin código.

**Test assertion post-fix**: `grep -r "MODELS_NOT_LOADED\|PIPELINE_TIMEOUT" wiki/05_modelo_datos.md` returns hits; ADR-016 existe y referencia ADR-015.

**Effort**: 1.5h.

---

#### G14 · CI runner para `requires_docker`
**Severidad**: HIGH (operational)
**Files**:
- `.github/workflows/test.yml` (nuevo)
- `AGENTS.md` — sección "Running tests" actualizar con `make test-rig`

**Findings que cierra**:
- pr-test HIGH #6 (no automated CI).

**Plan**:
1. GH Actions workflow con `services: postgres:16-alpine`, run `pytest -m "not requires_gpu and not requires_ffmpeg"`.
2. Markers documented en `AGENTS.md`.
3. Branch protection rule (manual, post-merge).

**Effort**: 1.5h (workflow + smoke run).

---

## 3. Orden de ejecución y dependencias

```
Tier 1 (paralelizable):
  G1 (image upload)       — independiente
  G3 (FTS regconfig)      — independiente
  G5 (list/search params) — independiente

Tier 1 (secuencial):
  G2 (start_transcription split) → habilita G6 (tools file split)
  G4 (lookup helper)             → simplifica G6 refactor

Tier 2 (depende de Tier 1):
  G6 (tools split + serializers) — depende de G2 + G4
  G7 (bearer hash/parser)        — independiente
  G8 (type microfixes)           — depende de G6 (algunos hooks viven en archivos nuevos)
  G9 (session relocation)        — independiente

Tier 3:
  G11 (AC coverage)        — depende de G1, G2, G6 (paths cambian)
  G12 (ContextVars)        — depende de G2, G6
  G13 (wiki sync)          — independiente, hacer en paralelo
  G14 (CI runner)          — independiente

G10 (API error hygiene): independiente, opcional last
```

Sugerencia: arrancar con **G1 + G3 + G5 en paralelo** (tres agentes), luego **G2 + G4** secuencial pero rápido, luego G6 desbloqueando el resto. G13 y G14 pueden ir en cualquier momento.

---

## 4. Out of scope

- **Image upload UI/UX flow** — Capa 5 lo posee.
- **Cleanup-job para `upload_sessions` huérfanos** — RF-CACHE-04, deferido a Capa 5.
- **`regenerate_mcp_token` tool + revocación cascada de upload_sessions** — Capa 5 (sandinas STRAT).
- **Eliminación del legacy `/api/transcriptions`** — Capa 5 DoD §0.2.
- **Pydantic models para tool responses (TypedDict)** — code-reviewer MEDIUM #12, posponer a Capa 6 con OpenAPI.
- **Filename round-trip via `upload_sessions.original_filename`** — D-048 sidebar, posponer a Capa 5.

---

## 5. Prompt template para batch executor

Cuando se ejecute un grupo, el prompt al agente sigue este shape (parametrizable):

```
You are executing Group {GROUP_ID} of the Capa 4 review-fixes plan.

## Context

- Working dir: /Users/francobertoldi/Documents/Sandinas/IA-Tasks/IA-Tasks-Investigación-Estrategia/transcription-api
- Branch: feat/capa4-mcp
- Plan file: docs/sesiones/2026-05-06-capa4-review-fixes-plan.md (READ THIS FIRST, especially §2 group {GROUP_ID})
- Related spec: docs/sesiones/2026-05-06-capa4-mcp-spec.md (the contract)
- Wiki authoritative for code: wiki/ (do NOT touch unless plan §G13 explicitly says so)
- Project priority: Privacy > Simplicity > Transcription Quality > Performance > Cost

## Mission

Execute Group {GROUP_ID} as described in §2 of the plan. Stay strictly within the listed files. Do NOT touch other groups' files (avoid merge conflicts with parallel executors).

## Discipline (MANDATORY)

1. Read the plan §2.G{GROUP_ID} top-to-bottom before any edit.
2. TDD where applicable: write or strengthen the test FIRST (RED), then implement (GREEN), then refactor.
3. Each commit: atomic, conventional commit message format. Group fixes should land as 1-3 commits max.
4. Run pytest before commit. If `requires_docker` blocks (no testcontainer locally), explicitly skip and document in commit message.
5. Run ruff before commit. Pre-commit hook runs it; do NOT skip with --no-verify.
6. Update CHANGELOG entry under [Unreleased] if user-facing behavior changes (e.g., new endpoint, new error code).
7. If you discover a finding NOT in the plan §2.G{GROUP_ID} list, STOP and report — do not silently fix; user decides scope.

## Output

When done, report:
- Files changed (path + LOC delta)
- Tests added/strengthened
- Test result: pytest output summary (counts)
- Any plan deviation or new finding
- Suggested commit message(s)
```