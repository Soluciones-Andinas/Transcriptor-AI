# Módulo MCP — Requerimientos Funcionales (Servidor MCP y endpoints REST de soporte)

**Source flow**: [`FL-TRX-01`](../FL/FL-TRX-01.md), [`FL-MIN-01`](../FL/FL-MIN-01.md)
**Architecture**: [`02_arquitectura.md`](../02_arquitectura.md) §3 (componentes C, D), §4 (secuencia crítica)
**Data model**: [`05_modelo_datos.md`](../05_modelo_datos.md) §2, §3, §7, §8
**Hardening level**: Execution-Normative

## Tabla resumen

| ID | Título | Actor | Pre-condición | Entradas | Salidas |
|---|---|---|---|---|---|
| RF-MCP-00 | Contrato base del módulo MCP (transversal) | — | — | — | — |
| RF-MCP-01 | Tool `request_upload_url` | Bearer válido | Bearer activo | `kind, file_size_bytes, mime_type?, transcription_id?` | `{upload_url, upload_id, bearer, expires_at}` |
| RF-MCP-02 | Tool `start_transcription` | Bearer válido | upload uploaded | `upload_id, language?, num_speakers?, min_speakers?, max_speakers?` | `{transcription_id, status, cache_hit}` |
| RF-MCP-03 | Endpoint REST `POST /api/upload` (audio + image) | Bearer válido | upload session vigente | multipart `file` + bearer + nonce | `{ok, upload_id}` o `{ok, image_id}` |
| RF-MCP-04 | Tool `list_my_transcriptions` | Bearer válido | — | `limit?, offset?, sort?` | array paginado |
| RF-MCP-05 | Tool `search_my_transcriptions` | Bearer válido | — | `query, limit?` | resultados ranked |
| RF-MCP-06 | Tool `get_transcription` | Bearer válido | — | `transcription_id` | TranscriptionResult JSON |
| RF-MCP-07 | Resource `transcription://<id>` | Bearer válido | — | URI | TranscriptionResult JSON |
| RF-MCP-08 | Resource `transcription://<id>/images/<image_id>` | Bearer válido | — | URI | imagen binaria + metadata |
| RF-MCP-09 | Tool `delete_transcription` | Bearer válido | — | `transcription_id` | `{ok}` |
| RF-MCP-10 | Tool `get_user_info` | Bearer válido | — | — | user + bearer info |
| RF-MCP-11 | Auth middleware MCP | — | — | header `Authorization: Bearer <token>` | continúa o rechaza |

---

## RF-MCP-00: Contrato base del módulo MCP (transversal)

### Propósito

Anchor estable que define el contrato de superficie del módulo MCP — transporte, autenticación, scoping, naming, URL pública. Los demás RFs del módulo (RF-MCP-01..11) y los specs de capas posteriores que necesiten referenciar "el contrato MCP" lo hacen contra esta sección, no contra implementación específica.

Este RF NO describe un endpoint o tool concreto: define los invariantes que TODOS los endpoints + tools + resources del módulo deben cumplir.

### Transporte

- **Protocolo**: MCP Streamable HTTP (no stdio, no SSE deprecated). Decisión: [ADR-011](../ADR/ADR-011.md).
- **URL pública**: `${PUBLIC_BASE_URL}/mcp`. La variable `PUBLIC_BASE_URL` se configura en `.env` (default `http://localhost:8000`); el Claude del user llama a esa URL desde Claude Code o Claude Desktop.
- **Discovery**: el endpoint público `GET /auth/me` (RF-AUTH-06) devuelve el campo `mcp_url` ya construido para que la UI del paso `/mcp-setup` (RF-UI-02) muestre la config lista para copiar.

### Autenticación

- **Esquema**: `Authorization: Bearer <plaintext>` con bearer emitido por RF-AUTH-04 (primer login) o RF-AUTH-07 (regenerate). El `<plaintext>` es ~64 chars URL-safe; en DB se almacena solo el `SHA-256(plaintext)` como `token_hash`.
- **Validación**: implementada en RF-MCP-11 (middleware). Cada tool / resource / endpoint REST del módulo debe pasar por el middleware antes del handler.
- **Códigos de error**: `MCP_BEARER_INVALID` (401), `MCP_BEARER_REVOKED` (401). Nunca `403` — la ausencia de bearer válido se trata como "no autenticado", no "no autorizado".

### Per-user scoping

- **Mecanismo**: SQLAlchemy `do_orm_execute` event listener fail-closed ([ADR-015](../ADR/ADR-015.md), reemplaza [ADR-014](../ADR/ADR-014.md)). El middleware (RF-MCP-11) setea `session.info["user_id"] = bearer.user_id` post-validación; toda query subsecuente sobre per-user models recibe el filtro `WHERE user_id = X` automáticamente.
- **Garantía**: una tool / resource handler NO puede leakear datos de otros users aunque omita el `WHERE user_id = X` explícito en la query. Si la dependency de auth se omite por error, la query raise `ScopingNotArmedError` (no leak silencioso).
- **Bypass intencional**: solo para auth lookups y mantenimiento administrativo, vía `with bypass_scoping(session): ...`. Nunca dentro de un tool/resource handler de Capa 6.

> **Implementation note (Capa 4 G13)**: tool/resource handlers que operan sobre per-user models emiten queries ORM **sin** predicate `user_id` explícito. El listener ([ADR-015](../ADR/ADR-015.md)) AND-injecta `WHERE user_id = X` desde `db.info["user_id"]` armado por el bearer middleware en `scoped_session(user_id)` (`db/session.py`). [ADR-016](../ADR/ADR-016.md) agrega un startup classification guard que rehúsa arrancar el servicio si un modelo nuevo carece de `user_id` y no está allowlisted en `_NON_SCOPED_MODELS` — defensa en capas runtime + boot.

### Naming convention

| Elemento | Convención | Ejemplos |
|---|---|---|
| Tool name | `snake_case`, prefijo `_my_` para tools per-user; verbo + sustantivo | `request_upload_url`, `list_my_transcriptions`, `delete_transcription` |
| Resource URI | `transcription://<id>` o `transcription://<id>/images/<image_id>` (jerarquía de recursos del user) | RF-MCP-07, RF-MCP-08 |
| Error code | `<MODULO>_<CAUSA>` en SCREAMING_SNAKE_CASE; lista cerrada en `05_modelo_datos.md` §8 | `MCP_BEARER_INVALID`, `TRANSCRIPTION_NOT_FOUND` |
| Field name | `snake_case` siempre, también en payloads JSON al cliente MCP | `audio_hash`, `cache_hit`, `num_speakers` |

### Side effects en cada call autenticado

- **`mcp_bearers.last_used_at`**: bumped a `clock_timestamp()` en cada hit autenticado (RF-MCP-11 step 6). Best-effort: un fallo en el commit de este bump NO debe rechazar el request del user.
- **Logs estructurados**: cada tool / resource call emite log JSON con `request_id`, `user_id`, `tool_name` o `resource_uri`, `duration_ms`, `cache_hit` (si aplica), `error_code` (si falla).

### Out of scope del contrato base

- Long-running tools (jobs > timeout HTTP del client MCP). En v0.1 todos los tools son sync (ADR-003). Async + job_id pattern queda para v2.
- Streaming de outputs (server-sent events dentro del MCP transport). v0.1 retorna respuestas completas.
- Tool versioning. v0.1 expone una sola versión; cuando haya breaking changes, prefijar con `_v2` y mantener legacy 1 release.

### Test Traceability

Este RF no genera tests propios — es un contrato. Los tests aparecen en RF-MCP-11 (middleware) y en cada RF de tool/resource. La consistencia con este contrato se valida en code review.

### No Ambiguities Left

- **Forbidden assumptions**: no se asume que el cliente MCP soporte session resumability; cada call es independiente desde el lado server.
- **Closed decisions**: bearer en header (no en query string ni en body); SHA-256 hex como hash; scoping fail-closed por defecto.
- **Out of scope**: federation con otros providers MCP; bridging a stdio.

**TODO explicit = 0**.

---

## RF-MCP-11: Auth middleware MCP (transversal)

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-MCP-11 |
| Título | Validar bearer token en cada tool/resource call MCP |
| Actor primario | MCP Server |
| Prioridad | Alta |
| Severidad | Crítica |

### Process Steps

| # | Paso |
|---|---|
| 1 | Extraer header `Authorization: Bearer <token>` del request MCP |
| 2 | Hashear: `token_hash = SHA256(token)` hex |
| 3 | SELECT id, user_id, revoked_at FROM `mcp_bearers` WHERE `token_hash = ?` |
| 4 | Si no encontrado: 401 + `MCP_BEARER_INVALID` |
| 5 | Si `revoked_at IS NOT NULL`: 401 + `MCP_BEARER_REVOKED` |
| 6 | UPDATE `mcp_bearers.last_used_at = now()` (no awaited, fire-and-forget) |
| 7 | Adjuntar `user_id` al contexto del request para uso por tools |
| 8 | Continuar con tool handler |

### Typed Errors

| Código | HTTP | Causa |
|---|---|---|
| `MCP_BEARER_INVALID` | 401 | Token no existe en DB |
| `MCP_BEARER_REVOKED` | 401 | Token existió pero fue revocado |

### Gherkin

```gherkin
Scenario: Bearer válido pasa el middleware
  Given bearer activo en mcp_bearers
  When tool call con header Authorization: Bearer <plaintext>
  Then middleware adjunta user_id al contexto
    And la tool handler se ejecuta

Scenario: Bearer revocado
  Given bearer con revoked_at NOT NULL
  When tool call
  Then 401 + MCP_BEARER_REVOKED

Scenario: Bearer inexistente
  Given header Authorization: Bearer foo (no en DB)
  When tool call
  Then 401 + MCP_BEARER_INVALID

Scenario: Sin header
  Given request MCP sin Authorization
  When tool call
  Then 401 + MCP_BEARER_INVALID
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-MCP-11-pos-01 | Positivo |
| TP-MCP-11-neg-01 | Negativo (revocado) |
| TP-MCP-11-neg-02 | Negativo (inexistente) |
| TP-MCP-11-neg-03 | Negativo (sin header) |
| TP-MCP-11-cov-01 | Cobertura (last_used_at se actualiza) |

**TODO explicit = 0**.

---

## RF-MCP-01: Tool `request_upload_url`

### Inputs

| Campo | Tipo | Requerido | Validación |
|---|---|---|---|
| `kind` | string enum | Sí | `audio` o `image` |
| `file_size_bytes` | int | Sí | `> 0`, ≤ MAX_UPLOAD_MB para audio (default 500); ≤ MAX_IMAGE_UPLOAD_MB para image (default 25) |
| `mime_type` | string | No | Si `kind=image`, debe estar en `{image/png, image/jpeg, image/webp, image/gif}` |
| `transcription_id` | UUID | Si `kind=image` | Debe existir en `transcriptions` y pertenecer al user del bearer |

### Process Steps

| # | Paso |
|---|---|
| 1 | Validar inputs (RF-TRX-03 cubre la validación de audio) |
| 2 | Si `kind=image`: SELECT transcriptions WHERE id=transcription_id AND user_id=bearer.user_id; si no existe → `TRANSCRIPTION_NOT_FOUND` |
| 3 | Generar `upload_id` (UUID), `nonce` (32 chars random URL-safe), `bearer_for_upload` (32 chars random URL-safe). Computar `upload_bearer_hash = SHA-256(bearer_for_upload).hex()` |
| 4 | Construir `upload_url`: `<BASE_URL>/api/upload?session=<nonce>` (audio) o `/api/upload-image?session=<nonce>` (image) |
| 5 | INSERT upload_sessions (id=upload_id, user_id, bearer_id, nonce, **upload_bearer_hash**, kind, transcription_id?, expected_size_bytes, expected_mime_type?, expires_at = now() + 10 min, status='requested'). El plaintext `bearer_for_upload` NUNCA se persiste — solo su hash. |
| 6 | Emitir log `upload_url_requested(user_id, upload_id, kind)` |
| 7 | Responder `{upload_url, upload_id, bearer: bearer_for_upload, expires_at}`. El plaintext se entrega UNA SOLA VEZ al cliente MCP; si lo pierde, debe pedir un nuevo `request_upload_url`. |

### Typed Errors

| Código | HTTP | Causa |
|---|---|---|
| `INVALID_PARAMETER` | 400 | kind inválido, file_size <= 0, mime_type rechazado |
| `FILE_TOO_LARGE` | 413 | file_size_bytes > límite |
| `TRANSCRIPTION_NOT_FOUND` | 404 | kind=image y transcription_id no es del user |
| `MCP_BEARER_INVALID` / `_REVOKED` | 401 | RF-MCP-11 |

### Gherkin

```gherkin
Scenario: Request upload URL para audio MP4
  Given bearer válido del user X
  When tool request_upload_url(kind="audio", file_size_bytes=100000000)
  Then 200 con upload_url, upload_id, bearer, expires_at
    And existe row upload_sessions con kind='audio', status='requested', user_id=X

Scenario: Request upload URL para imagen asociada a transcript ajeno
  Given bearer del user X
    And transcription Y pertenece al user Z
  When tool request_upload_url(kind="image", transcription_id=Y, ...)
  Then 404 + TRANSCRIPTION_NOT_FOUND

Scenario: Archivo demasiado grande
  Given MAX_UPLOAD_MB=500
  When tool request_upload_url(kind="audio", file_size_bytes=600000000)
  Then 413 + FILE_TOO_LARGE
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-MCP-01-pos-01 | Positivo audio |
| TP-MCP-01-pos-02 | Positivo image (transcript propio) |
| TP-MCP-01-neg-01 | Negativo (transcript ajeno) |
| TP-MCP-01-neg-02 | Negativo (file too large) |
| TP-MCP-01-neg-03 | Negativo (mime no permitido) |

**TODO explicit = 0**.

---

## RF-MCP-02: Tool `start_transcription`

### Inputs

| Campo | Tipo | Requerido | Validación |
|---|---|---|---|
| `upload_id` | UUID | Sí | Debe existir en `upload_sessions` con `status='uploaded'`, `kind='audio'`, owner del bearer |
| `language` | string | No (default `"es"`) | ISO 639-1 |
| `num_speakers` | int | No (default `None`) | 1-16. **Hint hard** a pyannote: si se provee, fuerza ese número exacto de speakers (override de min/max). Drift D-084 (2026-05-11): el código de `start_transcription` acepta este parámetro pero la spec previa no lo documentaba. |
| `max_speakers` | int | No (default 8) | 1-16 |
| `min_speakers` | int | No (default 1) | 1 ≤ min ≤ max ≤ 16. Si `num_speakers` también está presente, este se ignora. |

### Process Steps

| # | Paso |
|---|---|
| 1 | Auth middleware (RF-MCP-11) → `user_id` |
| 2 | SELECT upload_sessions WHERE id=upload_id AND user_id=user_id AND kind='audio' |
| 3 | Si no encontrada: `UPLOAD_SESSION_NOT_FOUND` (404) |
| 4 | Si `status='consumed'`: `UPLOAD_SESSION_ALREADY_CONSUMED` (409) |
| 5 | Si `status='requested'` (no se hizo upload): `UPLOAD_SESSION_NOT_FOUND` (404) |
| 6 | Si `status='expired'` o `now > expires_at + UPLOAD_SESSION_GRACE_SECONDS`: `UPLOAD_SESSION_NOT_FOUND`. Default grace = 30s para tolerar clock skew cliente/servidor (G8.4 — configurable vía `settings.upload_session_grace_seconds`). |
| 7 | Adquirir lock global (RF-TRX-04) con timeout 5 s; si falla: `LOCK_BUSY` (503) |
| 8 | Ejecutar pipeline (RF-TRX-01 si miss, RF-TRX-02 si hit) sobre `<DATA_DIR>/uploads/<upload_id>/original.bin` |
| 9 | Persistir TranscriptionResult: INSERT transcriptions con `user_id, audio_hash, original_filename, original_size_bytes, duration_seconds, language, num_speakers, text, segments JSONB, metadata JSONB` |
| 10 | UPDATE upload_sessions SET status='consumed', consumed_at=now() |
| 11 | Borrar `<DATA_DIR>/uploads/<upload_id>/` (audio temporal) |
| 12 | Liberar lock |
| 13 | Emitir log `transcription_persisted(transcription_id, user_id)` |
| 14 | Responder `{transcription_id, status: 'completed', cache_hit: bool}` |

### Typed Errors

| Código | HTTP | Causa |
|---|---|---|
| `UPLOAD_SESSION_NOT_FOUND` | 404 | upload_id desconocido, expirado, no del user, o aún en `requested` |
| `UPLOAD_SESSION_ALREADY_CONSUMED` | 409 | start_transcription ya invocado para ese upload |
| `LOCK_BUSY` | 503 | Lock global ocupado |
| `INVALID_FORMAT` | 400 | ffmpeg falla (delegado en RF-TRX-03) |
| `CUDA_OOM`, `MODEL_FAILURE` | 500 | Pipeline (RF-TRX-05) |
| `INTERNAL_ERROR` | 500 | Excepción no clasificada |

### Gherkin

```gherkin
Scenario: Procesa upload uploaded
  Given upload_session id=U, user=X, status='uploaded', kind='audio'
  When tool start_transcription(upload_id=U)
  Then 200 con transcription_id
    And nuevo row en transcriptions con user_id=X, audio_hash=<sha256>
    And upload_sessions.status='consumed'

Scenario: Cache hit
  Given pipeline previo con mismo audio_hash dentro de 24h
  When start_transcription nuevamente con otro upload del mismo audio
  Then 200 con cache_hit=true
    And total_duration_ms < 10000
    And nuevo row en transcriptions (histórico per-user)

Scenario: Upload de otro user
  Given upload_session pertenece al user Y
    And bearer es del user X
  When start_transcription(upload_id)
  Then 404 + UPLOAD_SESSION_NOT_FOUND
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-MCP-02-pos-01 | Positivo cache miss |
| TP-MCP-02-pos-02 | Positivo cache hit |
| TP-MCP-02-neg-01 | Negativo (upload ajeno) |
| TP-MCP-02-neg-02 | Negativo (already consumed) |
| TP-MCP-02-neg-03 | Negativo (lock busy) |

**TODO explicit = 0**.

---

## RF-MCP-03: Endpoint REST POST /api/upload (audio + image)

### Execution Sheet

| ID | RF-MCP-03 |
|---|---|
| Actor | Cliente HTTP (Claude Code via Bash, o UI futura) |

### Process Steps (común para audio e imagen)

| # | Paso |
|---|---|
| 1 | Recibir multipart `file` + query `session=<nonce>` + header `Authorization: Bearer <upload_bearer>` |
| 2 | SELECT upload_sessions WHERE nonce=? AND status='requested' |
| 3 | Validar `now < expires_at` |
| 4 | Validar `Authorization` bearer: computar `received_hash = SHA-256(plaintext del header).hex()` y comparar (constant-time, e.g. `hmac.compare_digest`) contra `upload_sessions.upload_bearer_hash`. Si no matchea: `MCP_BEARER_INVALID` (401). |
| 5 | Validar tamaño del archivo recibido ≤ `expected_size_bytes * 1.05` (margen 5%) |
| 6 | Si `kind='audio'`: guardar binario en `<DATA_DIR>/uploads/<upload_id>/original.bin` |
| 7 | Si `kind='image'`: validar mime real (file magic bytes) coincide con `expected_mime_type`; INSERT `images (transcription_id, user_id, filename, mime_type, size_bytes, file_path)`; mover binario a `<DATA_DIR>/blobs/<user_id>/<transcription_id>/<image_id>.<ext>` |
| 8 | UPDATE upload_sessions SET status='uploaded', uploaded_at=now() |
| 9 | Emitir log `upload_received(user_id, upload_id, size_bytes)` o `image_uploaded` |
| 10 | Responder `{ok: true, upload_id}` (audio) o `{ok: true, image_id}` (image) |

### Typed Errors

| Código | HTTP | Causa |
|---|---|---|
| `UPLOAD_SESSION_NOT_FOUND` | 404 | nonce desconocido o expirado |
| `MCP_BEARER_INVALID` | 401 | Bearer no coincide con la upload session |
| `INVALID_PARAMETER` | 400 | Tamaño muy distinto al esperado, mime no coincide |
| `FILE_TOO_LARGE` | 413 | Tamaño excede el esperado más allá del margen |
| `INVALID_FORMAT` | 400 | mime real no coincide con declarado (image) |

### Gherkin

```gherkin
Scenario: Upload audio OK
  Given upload_session uploaded=requested, expires_at en 5 min
  When POST /api/upload con file MP4 + nonce + bearer correcto
  Then 200 con upload_id
    And /data/uploads/<upload_id>/original.bin existe
    And upload_sessions.status='uploaded'

Scenario: Bearer wrong
  Given upload_session creada con bearer_for_upload=A
  When POST /api/upload con bearer=B
  Then 401

Scenario: Tamaño dispar
  Given expected_size_bytes=100MB
  When POST /api/upload con archivo 200MB
  Then 413 + FILE_TOO_LARGE
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-MCP-03-pos-01 | Positivo audio |
| TP-MCP-03-pos-02 | Positivo image |
| TP-MCP-03-neg-01 | Negativo bearer wrong |
| TP-MCP-03-neg-02 | Negativo session expired |
| TP-MCP-03-neg-03 | Negativo size mismatch |
| TP-MCP-03-neg-04 | Negativo mime fake |

**TODO explicit = 0**.

---

## RF-MCP-04: Tool `list_my_transcriptions`

### Inputs

| Campo | Tipo | Default |
|---|---|---|
| `limit` | int | 20, max 100 |
| `offset` | int | 0 |
| `sort` | string | `created_at_desc`; valores `created_at_desc`, `created_at_asc`, `duration_desc` |

### Process Steps

| # | Paso |
|---|---|
| 1 | Auth middleware → user_id |
| 2 | SELECT id, original_filename, duration_seconds, language, num_speakers, created_at FROM transcriptions WHERE user_id=? AND deleted_at IS NULL ORDER BY <sort> LIMIT ? OFFSET ? |
| 3 | SELECT count(*) WHERE user_id=? AND deleted_at IS NULL (para paginación) |
| 4 | Emitir log `transcription_listed(user_id, count)` |
| 5 | Responder `{items: [...], total, limit, offset}` |

### Gherkin

```gherkin
Scenario: User con 5 transcripciones
  When tool list_my_transcriptions()
  Then 200 con items.length = 5, total=5, sorted por created_at desc

Scenario: Paginación
  Given user con 30 transcripciones
  When tool list_my_transcriptions(limit=10, offset=20)
  Then items.length = 10
    And total = 30

Scenario: Cross-user isolation
  Given user A con 5 transcripciones, user B con 3
  When user A tool list_my_transcriptions()
  Then items.length = 5 (no ve las de B)
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-MCP-04-pos-01 | Positivo |
| TP-MCP-04-pos-02 | Positivo (paginación) |
| TP-MCP-04-neg-01 | Negativo (sin auth) |
| TP-MCP-04-cov-01 | Cobertura cross-user isolation |

**TODO explicit = 0**.

---

## RF-MCP-05: Tool `search_my_transcriptions`

### Inputs

| Campo | Tipo | Default |
|---|---|---|
| `query` | string | requerido, non-empty, max 200 chars |
| `limit` | int | 10, max 50 |

### Process Steps

| # | Paso |
|---|---|
| 1 | Auth middleware → user_id |
| 2 | SELECT id, original_filename, duration_seconds, ts_rank(...) AS rank FROM transcriptions WHERE user_id=? AND deleted_at IS NULL AND to_tsvector('spanish', text) @@ plainto_tsquery('spanish', $query) ORDER BY rank DESC LIMIT ? |
| 3 | Emitir log `transcription_searched(user_id, query, count)` |
| 4 | Responder array de `{id, original_filename, duration_seconds, snippet, rank}` |

### Gherkin

```gherkin
Scenario: Búsqueda con match
  Given user con transcripción cuyo text contiene "arquitectura microservicios"
  When tool search_my_transcriptions(query="arquitectura")
  Then 200 con items que incluyen esa transcripción
    And cada item tiene rank > 0

Scenario: Sin resultados
  When query="palabraqueesnoexiste"
  Then 200 con items=[]

Scenario: Cross-user isolation
  When user A search "x"
  Then no aparecen transcripciones de user B aunque matcheen
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-MCP-05-pos-01 | Positivo (match) |
| TP-MCP-05-pos-02 | Positivo (no match) |
| TP-MCP-05-cov-01 | Cobertura cross-user isolation |

**TODO explicit = 0**.

---

## RF-MCP-06: Tool `get_transcription`

### Inputs

| Campo | Tipo |
|---|---|
| `transcription_id` | UUID |

### Process Steps

| # | Paso |
|---|---|
| 1 | Auth middleware → user_id |
| 2 | SELECT * FROM transcriptions WHERE id=? AND user_id=? AND deleted_at IS NULL |
| 3 | Si no encontrado: `TRANSCRIPTION_NOT_FOUND` (404) |
| 4 | SELECT id, filename, caption FROM images WHERE transcription_id=? AND deleted_at IS NULL |
| 5 | Construir TranscriptionResult JSON: campos del registro + `images: [...]` |
| 6 | Responder JSON |

### Gherkin

```gherkin
Scenario: Get transcription propia
  Given transcription T del user X
  When user X tool get_transcription(T)
  Then 200 con TranscriptionResult JSON completo + images

Scenario: Get transcription ajena
  Given transcription T del user Y
  When user X tool get_transcription(T)
  Then 404 + TRANSCRIPTION_NOT_FOUND (no 403 para evitar info leak)

Scenario: Get transcription borrada
  Given transcription T con deleted_at NOT NULL
  When user X (owner) tool get_transcription(T)
  Then 404
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-MCP-06-pos-01 | Positivo |
| TP-MCP-06-neg-01 | Negativo (cross-user) |
| TP-MCP-06-neg-02 | Negativo (soft-deleted) |

**TODO explicit = 0**.

---

## RF-MCP-07: Resource `transcription://<id>`

### Process Steps

Equivalente a RF-MCP-06 pero servido como Resource MCP (no tool). El SDK MCP maneja URI parsing y devolución como resource.

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-MCP-07-pos-01 | Positivo (resource fetch) |
| TP-MCP-07-neg-01 | Negativo (cross-user) |

**TODO explicit = 0**.

---

## RF-MCP-08: Resource `transcription://<id>/images/<image_id>`

### Process Steps

| # | Paso |
|---|---|
| 1 | Auth middleware → user_id |
| 2 | SELECT * FROM images WHERE id=image_id AND transcription_id=transcription_id AND user_id=? AND deleted_at IS NULL |
| 3 | Si no encontrada: `IMAGE_NOT_FOUND` |
| 4 | Read file_path desde filesystem |
| 5 | Responder como MCP Resource binario con `mime_type` y caption en metadata |

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-MCP-08-pos-01 | Positivo (resource binary fetch) |
| TP-MCP-08-neg-01 | Negativo (cross-user) |
| TP-MCP-08-neg-02 | Negativo (image inexistente) |

**TODO explicit = 0**.

---

## RF-MCP-09: Tool `delete_transcription`

### Inputs

| Campo | Tipo |
|---|---|
| `transcription_id` | UUID |

### Process Steps

| # | Paso |
|---|---|
| 1 | Auth middleware → user_id |
| 2 | UPDATE transcriptions SET deleted_at = now() WHERE id=? AND user_id=? AND deleted_at IS NULL |
| 3 | Si rowcount == 0: `TRANSCRIPTION_NOT_FOUND` |
| 4 | UPDATE images SET deleted_at = now() WHERE transcription_id=? AND user_id=? |
| 5 | (Opcional) borrar binarios de imágenes en `<DATA_DIR>/blobs/.../` (cleanup posterior) |
| 6 | Emitir log `transcription_deleted(user_id, transcription_id)` |
| 7 | Responder `{ok: true}` |

### Special Cases

- Soft delete: el caché filesystem efímero NO se borra (es por audio_hash compartido). Solo el histórico per-user se marca.
- `cascading delete` de imágenes asociadas (también soft).

### Gherkin

```gherkin
Scenario: Delete propia
  Given transcription T del user X
  When user X tool delete_transcription(T)
  Then 200 con ok=true
    And transcriptions.deleted_at IS NOT NULL para T
    And images.deleted_at IS NOT NULL para imágenes de T

Scenario: Delete ajena
  Given transcription T del user Y
  When user X tool delete_transcription(T)
  Then 404 (no 403)

Scenario: Delete idempotente
  Given transcription ya deleted
  When mismo user delete otra vez
  Then 404
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-MCP-09-pos-01 | Positivo |
| TP-MCP-09-neg-01 | Negativo (cross-user) |
| TP-MCP-09-neg-02 | Negativo (idempotente) |
| TP-MCP-09-cov-01 | Cobertura cascade en images |

**TODO explicit = 0**.

---

## RF-MCP-10: Tool `get_user_info`

### Process Steps

| # | Paso |
|---|---|
| 1 | Auth middleware → user_id, bearer_id |
| 2 | SELECT email, display_name FROM users WHERE id=user_id |
| 3 | Responder `{user_id, email, display_name, bearer_id}` |

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-MCP-10-pos-01 | Positivo |

**TODO explicit = 0**.
