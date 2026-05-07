# SPEC-capa4-mcp-v1

> **Capa 4 — MCP Server (Streamable HTTP transport) + chunked upload pattern**
>
> **Fecha**: 2026-05-06
> **Source RFs**: RF-MCP-00..11 (anchor + tools + resources + middleware), RF-IMG (parcial: solo el endpoint `POST /api/upload-image` reutilizando RF-MCP-03 step pattern). Capa 3 RFs siguen vigentes — Capa 4 los envuelve.
> **Source ADRs**: [ADR-011](../../wiki/ADR/ADR-011.md) (MCP-first protocol), [ADR-013](../../wiki/ADR/ADR-013.md) (uploads HTTP con bearer), [ADR-015](../../wiki/ADR/ADR-015.md) (scoping fail-closed reusado tal cual).
> **Status**: Aprobado (defaults verificados empíricamente + cerrados por Franco 2026-05-06).
> **Hardening level**: Execution-Normative. TODO explicit = 0.

---

## 0. Alcance y dependencias

### 0.1 Lo que entra

- **MCP Server** con transport **Streamable HTTP** (`mcp[server]>=1.5,<2.0`, oficial Anthropic Python SDK), montado como ASGI sub-app en `app.mount("/mcp", mcp_app)` dentro del FastAPI ya existente. Single-process, comparte event loop + DB engine + settings con el resto del servicio.
- **9 tools MCP** (RF-MCP-01..02, RF-MCP-04..06, RF-MCP-09..10): `request_upload_url`, `start_transcription`, `list_my_transcriptions`, `search_my_transcriptions`, `get_transcription`, `delete_transcription`, `get_user_info`. Y dos handlers MCP que operan como variantes de upload (audio vs imagen) sobre el mismo `request_upload_url(kind, ...)`.
- **2 resources MCP** (RF-MCP-07..08): `transcription://<id>` (JSON completo) y `transcription://<id>/images/<image_id>` (binario).
- **Middleware MCP** (RF-MCP-11): valida `Authorization: Bearer <plaintext>`, hashea SHA-256, lookup en `mcp_bearers.token_hash`, arma `db.info["user_id"]` para que el listener fail-closed (ADR-015) inyecte el `WHERE user_id = X` automáticamente en cada query del handler.
- **Endpoint REST chunked-upload** `POST /api/upload` (RF-MCP-03) con `?session=<nonce>` + `Authorization: Bearer <ephemeral_plaintext>`. Valida hash contra `upload_sessions.upload_bearer_hash` (D-044, columna nueva agregada en Batch 0). Soporta `kind=audio` (escribe a `<DATA_DIR>/uploads/<upload_id>/original.bin`) y `kind=image` (escribe a `<DATA_DIR>/blobs/<user_id>/<transcription_id>/<image_id>.<ext>` + INSERT row en `images`).
- **Discovery del MCP url** desde `GET /auth/me` agregando campo `mcp_url = ${PUBLIC_BASE_URL}/mcp` (ya cubierto por Capa 2 RF-AUTH-06; en Capa 4 verificamos consistencia).
- **Reuse total** de la pipeline de Capa 3: `pipeline.orchestrator.orchestrate(...)` se llama desde la tool `start_transcription` con los mismos kwargs (lock + timeout + bypass_scoping para el INSERT). El primitive `_orchestrator_lock` no se toca.
- **Alembic migration** `add_upload_bearer_hash` agregando columna `upload_bearer_hash TEXT NOT NULL` a `upload_sessions` (D-044-impl).
- **Deprecation** del endpoint legacy `POST /api/transcriptions` (Capa 3): `deprecated=True` en OpenAPI + log WARN cuando se invoca. Sigue funcionando en Capa 4 para no romper el smoke-test del rig; **removal en Capa 5**.

### 0.2 Lo que NO entra (deferido a Capa 5+)

- UI React (`/mcp-setup`, `/auth/me`, transcripción listing). RF-UI-* son Capa 5.
- Tool `attach_image` separada (D-043: el RF unifica via `request_upload_url(kind="image", transcription_id=...)`; el ADR-011 pre-refactor sí la mencionaba pero el RF gana).
- Resource `user://me/transcriptions` (D-043: `list_my_transcriptions` tool cubre el caso).
- Tool `regenerate_mcp_token` desde MCP (rotación de bearer): Capa 2 ya lo expone como REST `POST /auth/regenerate-mcp-token`; no se duplica en MCP por seguridad — rotación requiere autenticación web (cookie de sesión), no un MCP bearer que podría estar comprometido.
- Removal del endpoint legacy `POST /api/transcriptions`.
- Cleanup de `upload_sessions` vencidas (RF-CACHE-04). El RF existe; Capa 4 deja la columna `expires_at` lista pero el cleanup-job que purga sesiones vencidas vive en Capa 5.
- Tool versioning (RF-MCP-00 §Out of scope): v0.1 expone una sola versión.
- Streaming de outputs MCP (long-running tools con progreso). v0.1 retorna respuestas completas.

### 0.3 Decisiones cerradas (defaults Franco 2026-05-06)

| Decisión | Valor | Justificación |
|---|---|---|
| **MCP SDK Python** | `mcp[server]>=1.5,<2.0` (Anthropic official) | Único SDK Python con Streamable HTTP transport production-grade. Roll-your-own = 2+ semanas perdidas. |
| **Mount strategy** | `FastMCP(...).streamable_http_app()` montada como ASGI sub-app en `app.mount("/mcp", mcp_asgi_app)` post `include_router` calls | Single-process, comparte event loop, comparte `db.engine`, comparte settings. ADR-011 declara explícitamente `GET /mcp` como endpoint público. |
| **`upload_bearer_hash` column** | Agregada en Batch 0 (Opción A: SHA-256 hex, columna NOT NULL en `upload_sessions`) | D-044. Privacy > Simplicity. Coherente con `mcp_bearers.token_hash`. Sin esta columna, RF-MCP-03 step 4 es inimplementable. |
| **Legacy `POST /api/transcriptions`** | `deprecated=True` en OpenAPI **ahora**; log WARN al invocarse; **removal en Capa 5** | Sigue funcionando para no romper el smoke-test del rig. El flow MCP-driven Capa 4 lo reemplaza funcionalmente. |
| **FTS GIN index** | NO crear migration nueva (ya existe `idx_transcriptions_text_fts` desde Capa 1) | Verificado en `alembic/versions/352c7acf6f15_initial_schema.py:133-134`. RF-MCP-05 puede usar `to_tsvector('spanish', text) @@ plainto_tsquery(...)` directo sin batch extra. |
| **Lock para start_transcription** | Reusar `pipeline.orchestrator.orchestrate(...)` sin tocar el primitive `_orchestrator_lock` | Mismo invariante: una pipeline a la vez en GPU. La tool MCP wrapea `orchestrate()` que ya hace acquire/timeout/release con BPO-31647 fix. |
| **Storage layout** | Usar paths existentes: `<DATA_DIR>/uploads/<upload_id>/original.bin` (transient), `<DATA_DIR>/blobs/<user_id>/<transcription_id>/<image_id>.<ext>` (persistent). El cache `<DATA_DIR>/cache/<user_id>/<audio_hash>/result.json` se mantiene como hoy | `settings.uploads_dir` y `settings.blobs_dir` ya existen como computed properties; solo agregar `mkdir(blobs_dir)` en lifespan. |
| **Auth en MCP context** | Middleware FastMCP (mecanismo del SDK) que extrae bearer del request context y arma `db.info["user_id"]` antes de invocar el tool handler. Tool handlers reciben `user_id` y `session` por context | FastMCP no usa FastAPI Depends; cada tool maneja su sesión DB. La indirección por context manager `mcp_request_session(user_id)` factoriza el setup. |
| **DB session lifecycle en tool handlers** | Cada tool/resource handler abre y cierra su propia `AsyncSession` via context manager (`async with mcp_request_session(user_id) as db`); commit en path feliz, rollback en except | FastMCP no tiene equivalente a `Depends(get_session)`. Patrón explícito pero limpio: igual contrato que `pipeline.orchestrator.orchestrate` (transaction owner = caller). |
| **Pagination** | offset + limit (no cursor), max 100 items por page para `list`, max 50 para `search` | RF-MCP-04 + RF-MCP-05 lo especifican literalmente. Cursor más correcto pero v0.1 no lo necesita (volumen Sandinas: ~5 transcripciones/día/user). |
| **Search ranking** | Postgres FTS: `to_tsvector('spanish', text) @@ plainto_tsquery('spanish', $query)` ordenado por `ts_rank` desc | RF-MCP-05 lo especifica; el GIN index ya existe. Snippet generation: usar `ts_headline('spanish', text, query, 'MaxWords=20, MinWords=5')` para retornar contexto matched (~50 chars). |
| **Resource URI parsing** | El SDK MCP maneja `transcription://<id>` y `transcription://<id>/images/<image_id>` via patterns registrados con `@mcp.resource(...)`. UUIDs se parsean con `uuid.UUID(...)` (raise ValueError → 400 INVALID_PARAMETER) | RF-MCP-07/08. Resource handlers son thin wrappers sobre las tools `get_transcription` / image fetch. |

### 0.4 Drifts vs wiki (loguear durante implementación)

- **D-044-impl**: la migration alembic `add_upload_bearer_hash` se crea en Batch 0. Si el operador del rig actualiza la imagen pero no corre `alembic upgrade head`, `request_upload_url` falla en el INSERT con NOT NULL violation. Documentar en deployment runbook (Capa 7).
- **Drift potencial RF-CACHE-03 ↔ código**: el RF todavía menciona `meta.json` corrupto pero el código usa `result.json` único + mtime TTL. No bloquea Capa 4; queda como sesión wiki dedicada futura (ya marcado en `docs/sesiones/2026-05-05-wiki-drifts.md` "Drifts pendientes").
- **AC-14 voice-path validation**: Capa 3 cerró con AC-14 deferida (silent path validado, voice path no). Capa 4 NO depende de eso para pasar sus ACs propios — el flow MCP es agnóstico al contenido del audio. Pero el primer end-to-end real con audio con voz (Capa 4 Batch 7 smoke en rig) es la oportunidad para cerrar AC-14 de Capa 3.

---

## 1. Tools, Resources y Endpoints (contract surface)

### 1.1 Tool list (autoritative: RF-MCP-01..10)

| Tool name | Inputs | Output | RF |
|---|---|---|---|
| `request_upload_url` | `kind: "audio"\|"image"`, `file_size_bytes: int`, `mime_type?: str`, `transcription_id?: UUID` | `{upload_url, upload_id, bearer, expires_at}` | [RF-MCP-01](../../wiki/RF/RF-MCP.md#rf-mcp-01-tool-request_upload_url) |
| `start_transcription` | `upload_id: UUID`, `language?: str="es"`, `min_speakers?: int=1`, `max_speakers?: int=8` | `{transcription_id, status: "completed", cache_hit: bool}` | [RF-MCP-02](../../wiki/RF/RF-MCP.md#rf-mcp-02-tool-start_transcription) |
| `list_my_transcriptions` | `limit?: int=20`, `offset?: int=0`, `sort?: str="created_at_desc"` | `{items: [...], total, limit, offset}` | [RF-MCP-04](../../wiki/RF/RF-MCP.md#rf-mcp-04-tool-list_my_transcriptions) |
| `search_my_transcriptions` | `query: str`, `limit?: int=10` | `[{id, original_filename, duration_seconds, snippet, rank}, ...]` | [RF-MCP-05](../../wiki/RF/RF-MCP.md#rf-mcp-05-tool-search_my_transcriptions) |
| `get_transcription` | `transcription_id: UUID` | `TranscriptionResult` JSON completo + `images: [...]` | [RF-MCP-06](../../wiki/RF/RF-MCP.md#rf-mcp-06-tool-get_transcription) |
| `delete_transcription` | `transcription_id: UUID` | `{ok: true}` | [RF-MCP-09](../../wiki/RF/RF-MCP.md#rf-mcp-09-tool-delete_transcription) |
| `get_user_info` | `()` | `{user_id, email, display_name, bearer_id}` | [RF-MCP-10](../../wiki/RF/RF-MCP.md#rf-mcp-10-tool-get_user_info) |

Naming: `snake_case`, prefijo `_my_` para tools per-user (RF-MCP-00 §Naming convention).

### 1.2 Resources (autoritative: RF-MCP-07..08)

| URI pattern | Output | RF |
|---|---|---|
| `transcription://<id>` | Mismo JSON que `get_transcription` (TranscriptionResult + images metadata) | [RF-MCP-07](../../wiki/RF/RF-MCP.md#rf-mcp-07-resource-transcriptionid) |
| `transcription://<id>/images/<image_id>` | Imagen binaria con `mime_type` + caption en metadata | [RF-MCP-08](../../wiki/RF/RF-MCP.md#rf-mcp-08-resource-transcriptionidimagesimage_id) |

Resources son thin wrappers sobre las tools (mismo scoping fail-closed via middleware). El cliente MCP (Claude Code/Desktop) los resuelve cuando el user los referencia en el chat.

### 1.3 REST endpoint `POST /api/upload` (RF-MCP-03)

Auth: bearer ephemeral del upload session (NO el bearer principal del user). Header `Authorization: Bearer <plaintext_ephemeral>`; query `?session=<nonce>`.

**Input** (multipart/form-data):

| Campo | Tipo | Notas |
|---|---|---|
| `file` | UploadFile (binary) | Audio: mp4/mp3/m4a/wav/flac. Imagen: png/jpg/webp/gif. |

**Validación** (en orden):
1. SELECT `upload_sessions` WHERE `nonce=?` AND `status='requested'`. 404 `UPLOAD_SESSION_NOT_FOUND` si no existe.
2. `now < expires_at`. 404 `UPLOAD_SESSION_NOT_FOUND` si expiró.
3. `received_hash = SHA-256(plaintext del header).hex()`; `hmac.compare_digest(received_hash, upload_sessions.upload_bearer_hash)`. 401 `MCP_BEARER_INVALID` si no matchea.
4. Tamaño recibido ≤ `expected_size_bytes * 1.05` (margen 5%). 413 `FILE_TOO_LARGE`.
5. Si `kind="image"`: leer magic bytes, validar mime real ≡ `expected_mime_type`. 400 `INVALID_FORMAT` si difiere.

**Output** (200 OK):

```json
// audio
{"ok": true, "upload_id": "uuid"}
// image
{"ok": true, "image_id": "uuid"}
```

**Side effects**:
- `kind="audio"`: escribe binario a `<DATA_DIR>/uploads/<upload_id>/original.bin`. UPDATE `upload_sessions` SET `status='uploaded'`, `uploaded_at=now()`.
- `kind="image"`: INSERT `images (id, transcription_id, user_id, filename, mime_type, size_bytes, file_path)`; mueve binario a `<DATA_DIR>/blobs/<user_id>/<transcription_id>/<image_id>.<ext>`. UPDATE `upload_sessions` igual.

### 1.4 Endpoint MCP transport `<PUBLIC_BASE_URL>/mcp`

URL pública declarada en RF-MCP-00 + ADR-011. Accept GET para handshake del transport (Streamable HTTP), POST para tool/resource calls. El SDK lo maneja todo; la app solo monta el sub-app ASGI.

### 1.5 Extensión `GET /auth/me` (verificación, no nuevo endpoint)

Capa 2 ya retorna `mcp_url` en el body. Capa 4 verifica que el valor sea consistente con `${PUBLIC_BASE_URL}/mcp` (no que apunte a un placeholder). AC-13 lo cubre.

### 1.6 Lifespan: agregar `mkdir(settings.blobs_dir)` (1 línea)

`main.py` lifespan ya hace `mkdir` para `models_dir`, `cache_dir`, `uploads_dir`. Agregar `blobs_dir` para que el primer `POST /api/upload?kind=image` no falle con `FileNotFoundError`.

---

## 2. Main Flow (canonical use case end-to-end)

User: Franco, Claude Code conectado al MCP del rig.

```
Cliente Claude (Code/Desktop)
   │
   │  ① tool request_upload_url(kind="audio", file_size_bytes=10MB)
   ▼
[GET /mcp Streamable HTTP]
   │
   │  middleware MCP-11: validar bearer, armar db.info["user_id"]
   ▼
mcp/tools.py::request_upload_url
   │
   │  bearer_for_upload = secrets.token_urlsafe(32)
   │  upload_bearer_hash = sha256(bearer_for_upload).hex()
   │  INSERT upload_sessions (status='requested', upload_bearer_hash, ...)
   │
   ▼
   ◄── {upload_url: "/api/upload?session=<nonce>", upload_id, bearer: bearer_for_upload, expires_at}

Cliente Claude
   │
   │  ② POST /api/upload?session=<nonce>  (multipart file + Authorization: Bearer <plaintext>)
   ▼
api/upload.py::upload_audio
   │
   │  validate (nonce + expires_at + hmac.compare_digest(hash) + size + magic bytes)
   │  write to <DATA_DIR>/uploads/<upload_id>/original.bin
   │  UPDATE upload_sessions SET status='uploaded'
   │
   ▼
   ◄── {ok: true, upload_id}

Cliente Claude
   │
   │  ③ tool start_transcription(upload_id, language="es", max_speakers=4)
   ▼
[GET /mcp]
   │
   │  middleware: validar bearer principal, armar db.info["user_id"]
   ▼
mcp/tools.py::start_transcription
   │
   │  SELECT upload_sessions WHERE id=? AND user_id=? AND status='uploaded'
   │  → row encontrada (filter implícito por listener fail-closed)
   │
   │  await orchestrate(  # reuse Capa 3
   │      user_id=user_id,
   │      db=db,
   │      file_path=Path("/data/uploads/<upload_id>/original.bin"),
   │      ... whisper, pyannote, cache_store, upload_dir, language, max_speakers ...
   │  )
   │
   │  // dentro de orchestrate(): lock acquire + normalize + cache lookup
   │  //                          + STT + diarize + merge + INSERT transcriptions
   │  //                          + cleanup WAV + lock release
   │
   │  UPDATE upload_sessions SET status='consumed', consumed_at=now()
   │  shutil.rmtree(<DATA_DIR>/uploads/<upload_id>/)  // borrar audio temporal
   │
   ▼
   ◄── {transcription_id, status: "completed", cache_hit: false}

Cliente Claude
   │
   │  ④ tool get_transcription(transcription_id)
   ▼
mcp/tools.py::get_transcription
   │
   │  SELECT transcriptions WHERE id=? (listener inyecta WHERE user_id=X)
   │  SELECT images WHERE transcription_id=? AND deleted_at IS NULL
   │  → ensamblar TranscriptionResult JSON con segments + images metadata
   │
   ▼
   ◄── { transcription_id, segments: [...], text_content: "...", num_speakers: N, images: [...], metadata: {...} }
```

### 2.1 Postcondiciones del happy path

- `transcriptions` row existe con `user_id = bearer.user_id`, `audio_hash` SHA-256 del PCM puro (D-038), `text_content` con segmentos `SPEAKER_xx: ...` newline-separated.
- `upload_sessions.status = 'consumed'`, `consumed_at IS NOT NULL`.
- `<DATA_DIR>/uploads/<upload_id>/` no existe (borrado).
- `<DATA_DIR>/cache/<user_id>/<audio_hash>/result.json` existe (cache populated).
- Logs estructurados emitidos: `request_upload_url_emitted`, `upload_received`, `transcription_persisted`, `mcp_request_completed` para cada call con `user_id`, `request_id`, `tool_name`, `duration_ms`, `cache_hit`.
- `mcp_bearers.last_used_at` bumped al timestamp del último call (best-effort, no bloqueante).

---

## 3. Acceptance Criteria

| ID | Given / When / Then | Cubre |
|---|---|---|
| **AC-1** | Given bearer válido + audio 30 s mp3, When `request_upload_url(kind=audio, size=300000)` → POST /api/upload → `start_transcription(upload_id)`, Then 200 con `{transcription_id, status: "completed", cache_hit: false}` y row en `transcriptions` con `user_id = bearer.user_id`. | RF-MCP-01 + 02 + 03 |
| **AC-2** | Given dos bearers distintos (user A y user B) + el MISMO audio, When user A hace el flow completo y luego user B repite, Then ambos terminan con `cache_hit: false` (cache es per-user, D-027). | RF-MCP-02 + ADR-015 |
| **AC-3** | Given user A con 5 transcriptions, user B con 3, When user A llama `list_my_transcriptions()`, Then retorna `items.length == 5`, `total == 5`, ninguna fila de user B. | RF-MCP-04 + ADR-015 |
| **AC-4** | Given user con transcription cuyo `text_content` contiene "arquitectura microservicios", When `search_my_transcriptions(query="arquitectura")`, Then retorna esa transcription con `rank > 0` y `snippet` con la palabra matched. Búsqueda usa el GIN index existente. | RF-MCP-05 |
| **AC-5** | Given transcription T del user X, When user Y (otro bearer) llama `get_transcription(T)`, Then 404 `TRANSCRIPTION_NOT_FOUND` (mismo response shape que id inexistente — no leak de existence). | RF-MCP-06 + ADR-015 |
| **AC-6** | Given resource URI `transcription://<id>` con id propio, When el cliente MCP fetchea, Then el SDK retorna el JSON completo equivalente a `get_transcription`. Resource URI con id ajeno → mismo 404. | RF-MCP-07 |
| **AC-7** | Given image asociada a transcription T (subida via `request_upload_url(kind=image, transcription_id=T)` + POST /api/upload), When fetch resource `transcription://T/images/<image_id>`, Then retorna el binario con `mime_type` correcto. Image de otro user → 404 `IMAGE_NOT_FOUND`. | RF-MCP-08 + RF-IMG |
| **AC-8** | Given bearer revocado (`mcp_bearers.revoked_at IS NOT NULL`), When cualquier tool MCP, Then 401 `MCP_BEARER_REVOKED`. Bearer inexistente → 401 `MCP_BEARER_INVALID`. Sin header → 401 `MCP_BEARER_INVALID`. | RF-MCP-11 |
| **AC-9** | Given dos `start_transcription` concurrentes con audios distintos del mismo user, When el primero está dentro de `orchestrate()` con el lock GPU tomado, Then el segundo espera hasta `LOCK_WAIT_SECONDS`; si timeout, raise `LOCK_BUSY` (503 mapeado a MCP error). | RF-MCP-02 + reuse Capa 3 lock |
| **AC-10** | Given upload session que NO se consumió (cliente abandonó tras `request_upload_url`), When pasan ≥10 min sin llegar el POST /api/upload, Then `expires_at` está en el pasado y `start_transcription(upload_id)` retorna 404 `UPLOAD_SESSION_NOT_FOUND`. (El cleanup-job que MARCA `status='expired'` queda en Capa 5, RF-CACHE-04.) | RF-MCP-02 + RF-MCP-01 expires_at |
| **AC-11** | Given `delete_transcription(T)` invocado por owner de T, When la tool corre, Then `transcriptions.deleted_at IS NOT NULL`, `images.deleted_at IS NOT NULL` para cada imagen asociada (cascade soft-delete), y `get_transcription(T)` posterior → 404. Idempotencia: invocar dos veces → 404 la segunda. | RF-MCP-09 |
| **AC-12** | Given el MCP server arrancado, When el cliente Claude (Code/Desktop) se conecta a `${PUBLIC_BASE_URL}/mcp` con bearer válido en config, Then list-tools muestra los 7 tools y list-resources reconoce los 2 patterns. | ADR-011 + RF-MCP-00 |
| **AC-13** | Given cookie de sesión web, When `GET /auth/me`, Then body incluye `mcp_url` con valor `${PUBLIC_BASE_URL}/mcp`, no placeholder. | RF-AUTH-06 (reuse) |
| **AC-14** | Given tool MCP cualquiera autenticada, When la handler termina (ok o error), Then `mcp_bearers.last_used_at` bumped al timestamp del request (best-effort: si el UPDATE falla, el request del user igual retorna OK). | RF-MCP-00 §Side effects + RF-MCP-11 step 6 |
| **AC-15** | Given migration `add_upload_bearer_hash` aplicada, When se inspecciona el schema de `upload_sessions`, Then existe la columna `upload_bearer_hash TEXT NOT NULL` y los rows existentes (debería haber cero al deploy) cumplen NOT NULL. La migration falla si encuentra rows con NULL en pre-flight (defense). | D-044 |
| **AC-16** | Given el endpoint legacy `POST /api/transcriptions`, When un cliente lo invoca, Then el response es OK (sigue funcionando) y el log emite WARN `legacy_endpoint_invoked deprecated_endpoint=POST_/api/transcriptions removal_target=Capa5`. OpenAPI lo marca como `deprecated: true`. | Decisión Franco D-026 |

---

## 4. Errores tipados (Error catalog Capa 4)

| Code | HTTP | Cuándo | Body |
|---|---|---|---|
| `MCP_BEARER_INVALID` | 401 | Header `Authorization: Bearer` ausente, malformado, o token no encontrado en `mcp_bearers` | `{error_code, reason}` |
| `MCP_BEARER_REVOKED` | 401 | Token existe pero `revoked_at IS NOT NULL` | `{error_code, reason}` |
| `INVALID_PARAMETER` | 400 | `kind` inválido, `file_size_bytes <= 0`, `mime_type` rechazado, `min > max`, UUID malformada | `{error_code, reason}` |
| `FILE_TOO_LARGE` | 413 | Audio: `file_size_bytes > MAX_UPLOAD_MB`. Imagen: `> MAX_IMAGE_UPLOAD_MB`. Tamaño real recibido > `expected * 1.05` | `{error_code, reason, max_mb}` |
| `TRANSCRIPTION_NOT_FOUND` | 404 | `transcription_id` no existe, no es del user, o `deleted_at IS NOT NULL` | `{error_code, reason}` |
| `UPLOAD_SESSION_NOT_FOUND` | 404 | `upload_id` desconocido, expirado, no del user, o aún en `requested` (no se hizo upload) | `{error_code, reason}` |
| `UPLOAD_SESSION_ALREADY_CONSUMED` | 409 | `start_transcription` ya invocado para ese upload | `{error_code, reason}` |
| `IMAGE_NOT_FOUND` | 404 | image_id no existe, transcription_id no matchea, no es del user, o `deleted_at IS NOT NULL` | `{error_code, reason}` |
| `INVALID_FORMAT` | 400 | Magic bytes del archivo subido no matchean `expected_mime_type` (imagen) | `{error_code, reason}` |
| `LOCK_BUSY` | 503 | Lock GPU no adquirido en `LOCK_WAIT_SECONDS` (reuse Capa 3) | `{error_code, retry_after}` + Retry-After header |
| `MODELS_NOT_LOADED` | 503 | Whisper o pyannote no están en estado `ready` (reuse Capa 3) | `{error_code, reason, detail}` + Retry-After |
| `INTERNAL_ERROR` | 500 | Catch-all con `error_id` UUID para correlación en logs | `{error_code, reason: "see error_id in logs", error_id}` |

**Mapeo MCP transport**: errores HTTP del SDK MCP se entregan al cliente como JSON-RPC 2.0 errors con `code` numérico estándar + `data: {error_code, reason, ...}` para preservar la taxonomía. El SDK maneja la traducción.

---

## 5. Alternative flows

### ALT-1: Upload de imagen (kind="image")

`request_upload_url(kind="image", file_size_bytes=2_000_000, mime_type="image/png", transcription_id=T)`. Validación adicional: T debe pertenecer al user. POST /api/upload escribe a `blobs/<user_id>/<transcription_id>/<image_id>.png` y crea row en `images`. La imagen queda asociada a T desde el momento del upload — NO requiere tool separada `attach_image` (D-043, RF gana sobre ADR-011).

### ALT-2: Cache hit en `start_transcription`

Mismo flow que ALT-1 de Capa 3: `orchestrate()` detecta cache hit (clave `(user_id, audio_hash)`), persiste row nueva en `transcriptions` con `metadata.cache_hit=true`, retorna en <500ms. La tool MCP retorna `{transcription_id, status: "completed", cache_hit: true}`.

### ALT-3: Bearer revocado mid-session

Cliente Claude tiene bearer en config; user revoca via `POST /auth/regenerate-mcp-token`. Próximo tool call → 401 `MCP_BEARER_REVOKED`. El cliente Claude debe re-configurar con el bearer nuevo (de UI Capa 5 o por entrega manual). Sin retry automático en v0.1.

### ALT-4: Search sin resultados

`search_my_transcriptions(query="palabraqueesnoexiste")` → 200 con `items: []`. NO error.

### ALT-5: `list_my_transcriptions` con offset > total

`offset=100, total=20` → 200 con `items: []`, `total: 20`. NO error. El cliente paginador detecta el end via `len(items) < limit` o `offset >= total`.

### ALT-6: Resource URI con UUID malformada

`transcription://not-a-uuid` o `transcription://abc/images/xyz` → 400 `INVALID_PARAMETER`. El SDK MCP intercepta el ValueError del `uuid.UUID(...)` parsing.

### ALT-7: Concurrent `request_upload_url` del mismo user (sin lock)

No hay lock global aquí; `request_upload_url` solo hace INSERT idempotente con `nonce` único. Dos calls concurrentes generan dos rows distintas con dos `upload_id`s distintos. Sin race.

### ALT-8: `start_transcription` antes de POST /api/upload

`upload_sessions.status = 'requested'` (todavía no `uploaded`). RF-MCP-02 step 5 lo trata como `UPLOAD_SESSION_NOT_FOUND` (404). Cliente debe hacer POST /api/upload primero.

---

## 6. Data model deltas vs Capa 3

**Una sola migración en Batch 0** (`alembic revision -m "add_upload_bearer_hash"`):

```python
# alembic/versions/<rev>_add_upload_bearer_hash.py
def upgrade():
    op.add_column(
        'upload_sessions',
        sa.Column('upload_bearer_hash', sa.Text(), nullable=False),
    )
    # No backfill needed: zero rows en producción al deploy (la tabla existe pero
    # no se ha escrito desde Capa 1; Capa 3 no la usa).

def downgrade():
    op.drop_column('upload_sessions', 'upload_bearer_hash')
```

**Modelo ORM** (`db/models/upload_session.py`):

```python
upload_bearer_hash: Mapped[str] = mapped_column(Text, nullable=False)
```

Sin índice — el lookup es siempre por `nonce` (UNIQUE), no por hash.

---

## 7. Configuración (env vars nuevas)

| Variable | Default | Descripción |
|---|---|---|
| `MAX_UPLOAD_MB` | `500` | Cap audio (reuse Capa 3) |
| `MAX_IMAGE_UPLOAD_MB` | `25` | Cap imagen (Capa 4 nuevo) |
| `UPLOAD_SESSION_TTL_SECONDS` | `600` | 10 min entre `request_upload_url` y POST /api/upload |
| `MCP_TRANSPORT_PATH` | `/mcp` | Mount path del MCP server (ADR-011 fija `/mcp`) |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Reuse Capa 2; usado por el `mcp_url` en `/auth/me` |

Existentes que se usan tal cual: `LOCK_WAIT_SECONDS`, `LOCK_RETRY_AFTER_SECONDS`, `PIPELINE_TIMEOUT_SECONDS`, `CACHE_TTL_SECONDS`, `DATA_DIR`, `HF_TOKEN`, `COMPUTE_TYPE`, `WHISPER_MODEL`, `PYANNOTE_MODEL`.

---

## 8. Estructura de módulos

```
src/transcription_api/
├── mcp/                            # NUEVO en Capa 4
│   ├── __init__.py                 # exports mcp_app, register_tools, register_resources
│   ├── server.py                   # FastMCP() + mount factory
│   ├── middleware.py               # bearer extraction + db.info["user_id"] arming (RF-MCP-11)
│   ├── session.py                  # mcp_request_session(user_id) async ctx mgr (DB lifecycle)
│   ├── tools/
│   │   ├── __init__.py             # registers all tools
│   │   ├── upload.py               # request_upload_url
│   │   ├── transcription.py        # start_transcription, get_transcription, list_my_, search_my_, delete_
│   │   └── user.py                 # get_user_info
│   └── resources.py                # transcription://<id>, transcription://<id>/images/<image_id>
├── api/
│   ├── __init__.py                 # existente: transcriptions_router; agregar upload_router
│   ├── transcriptions.py           # existente; marcar deprecated=True (Capa 4 step)
│   └── upload.py                   # NUEVO: POST /api/upload (RF-MCP-03)
├── db/
│   └── models/
│       └── upload_session.py       # +upload_bearer_hash column
└── main.py                         # +app.mount("/mcp", mcp_app); +mkdir(blobs_dir) en lifespan; +deprecation log para transcriptions endpoint
```

**Importaciones críticas**:
- `mcp/middleware.py` reusa la lógica de `auth/dependencies.py::get_current_user_mcp` factorizada en `auth/mcp_bearer.py::verify_bearer` (que ya existe). El middleware FastMCP la llama y arma `db.info["user_id"]`.
- `mcp/tools/transcription.py::start_transcription` importa `pipeline.orchestrator.orchestrate` y lo invoca tal cual.
- `api/upload.py` factoriza el hash compare con `auth/mcp_bearer.py::compare_token_hash` (helper a agregar si no existe).

---

## 9. Out of scope (Capa 5+)

- UI React.
- Cleanup-job que MARCA `upload_sessions.status='expired'` post-TTL (RF-CACHE-04). Hoy `expires_at` se respeta en read-time pero no hay sweep.
- Tool `attach_image` separada (D-043, no se va a hacer).
- Resource `user://me/transcriptions` (D-043, no se va a hacer).
- Removal del legacy `POST /api/transcriptions`.
- Tool `regenerate_mcp_token` desde MCP.
- Streaming MCP (long-running tools con progreso parcial).
- Tool versioning (`_v2` prefix para breaking changes).
- Per-tool rate limiting o throttling.
- `last_used_at` throttle (D-020 backlog).
- Encryption-key rotation versioning (D-019 backlog).
- Test sub-app fixture refactor (D-021 backlog).
- Callback service extraction (D-022 backlog).

---

## 10. Riesgos y mitigaciones

| Riesgo | Probabilidad | Impacto | Mitigación |
|---|---|---|---|
| FastMCP SDK breaking change entre 1.5 y 1.6 | Media | Bloquea el server | Pin estricto `mcp[server]>=1.5,<2.0`; tests de smoke MCP validan handshake + tool call básico. CI corre con la versión pineada. |
| Auth en MCP context no propaga `user_id` correctamente al listener | Alta | Privacy leak (cross-user) | Test integration: bearer A llamando `list_my_transcriptions`, ver que NO retorna rows de bearer B. Ejecutar este test PRIMERO de los E2E (es el invariante crítico). Si el listener falla, rompe RF-MCP-11 inmediatamente. |
| ASGI mount path conflict con FastAPI routes | Baja | 404 inesperados | El `app.mount("/mcp", ...)` se registra DESPUÉS de `include_router` calls; FastAPI prioriza router routes sobre mounts. Test: `GET /mcp` devuelve handshake MCP (no 404 de FastAPI). |
| Búsqueda FTS con `ts_headline` lenta sobre `text_content` largo (transcripts de 1h) | Media | UX de search degradada | RF-MCP-05 cap a 50 results. Si `ts_headline` agrega >100ms, fallback a snippet pre-computado (truncar text_content a 200 chars matched offset ± 50). Decidir post-AC-4 con datos. |
| `request_upload_url` race: dos calls del mismo user ocupan VRAM en pipeline diferentes (stress test) | Baja | OOM | Lock GPU global (Capa 3) ya serializa. AC-9 lo verifica. |
| `upload_bearer_hash` confusión con `mcp_bearers.token_hash` (dos hashes distintos) | Baja | Bug de auth | Naming explícito en código + comentario en model + AC-15 verifica el schema. |
| Cliente MCP no manda el bearer en formato `Authorization: Bearer <token>` (diferentes implementaciones de SDK) | Baja | 401 falso | RF-MCP-11 step 1 acepta solo el formato canónico; RF-MCP-00 lo declara explícito. Test integration con el SDK Python `mcp` 1.5+ verifica el wire format. |
| Removal del legacy `POST /api/transcriptions` rompe el smoke-test del rig | Alta si removemos en Capa 4 | Bloquea releases | NO removemos en Capa 4 (Decisión Franco). Capa 5 lo remueve después de validar que el nuevo flow MCP funciona end-to-end con audio real. |
| Migración `add_upload_bearer_hash` corre en producción con rows pre-existentes en `upload_sessions` (NOT NULL violation) | Casi nula (tabla vacía hoy) | Migration aborta | Pre-flight check en la migration: `SELECT count(*) FROM upload_sessions WHERE upload_bearer_hash IS NULL` debe ser 0 antes del `ALTER TABLE`. Si hay rows, abortar y obligar al operator a limpiarlos. |
| Futuro modelo per-user agregado en Capa 5+ olvida la columna `user_id` (typo, rename a `owner_id`, copy/paste de un modelo global) | Media (en cuanto crezca el equipo) | Privacy leak silencioso: listener no enrolla el modelo, queries cross-user devuelven todas las filas sin warning | **Capa 4 hardening**: `db.scoping._validate_model_classification()` invocado en `enable_per_user_scoping()` itera todos los mappers de `Base.registry` y assertea que cada uno tiene `user_id` O está en `_NON_SCOPED_MODELS = {"User"}` (allowlist explícita de globales). Modelo en ninguno → `ScopingClassificationError` en startup → service refuse to start con mensaje accionable. Cierra la fail-OPEN trap del listener antes de que un futuro PR la materialice. Test smoke `test_validate_real_registry_classifies_cleanly` actúa como contract a nivel suite: si alguien agrega un modelo sin `user_id` y sin entrada en allowlist, el test rojea inmediatamente. |

---

## 11. Trazabilidad

| Wiki / ADR | Cubierto en spec |
|---|---|
| RF-MCP-00 (contract base) | §0.1, §0.3 (mount strategy + naming + scoping fail-closed reuse), §1.1 |
| RF-MCP-01 (request_upload_url) | §1.1, §1.3 (link al endpoint de upload), AC-1, AC-15 |
| RF-MCP-02 (start_transcription) | §1.1, §2 main flow, AC-1, AC-2, AC-9, AC-10 |
| RF-MCP-03 (POST /api/upload) | §1.3, §2 main flow, AC-1, AC-7, §4 errores |
| RF-MCP-04 (list_my_transcriptions) | §1.1, AC-3 |
| RF-MCP-05 (search_my_transcriptions) | §1.1, §0.3 (FTS GIN index existente), AC-4 |
| RF-MCP-06 (get_transcription) | §1.1, §2 main flow, AC-5 |
| RF-MCP-07 (resource transcription://<id>) | §1.2, AC-6 |
| RF-MCP-08 (resource ./images/<id>) | §1.2, AC-7 |
| RF-MCP-09 (delete_transcription) | §1.1, AC-11 |
| RF-MCP-10 (get_user_info) | §1.1 |
| RF-MCP-11 (auth middleware) | §1.4, §0.3 (auth en MCP context), AC-8, AC-14 |
| RF-IMG (parcial) | §1.3 (kind=image branch), ALT-1, AC-7 |
| ADR-011 (MCP-first protocol) | §0.1 (mount + transport), §1.4 |
| ADR-013 (uploads HTTP con bearer) | §1.3 (ephemeral bearer pattern), §6 (upload_bearer_hash) |
| ADR-015 (scoping fail-closed) | §0.3 (auth context), AC-2, AC-3, AC-5 |
| D-043 (ADR-011 ↔ RF-MCP unified upload) | §0.2 (no `attach_image`), §0.4, ALT-1 |
| D-044 (upload_bearer_hash column) | §0.3, §6, AC-15 |

---

## 12. Definition of Done

- [ ] **16/16 ACs verdes** en pytest (unit + integration + e2e). E2E corre el flow completo `request_upload_url → POST /api/upload → start_transcription → get_transcription` con bearer dev.
- [ ] **Migration `add_upload_bearer_hash` aplicada** en el rig vía `alembic upgrade head`; `\d upload_sessions` muestra la columna NOT NULL.
- [ ] **Cliente Claude Code** (Franco) puede agregar el MCP server a su `~/.claude/mcp.json` con la URL `${PUBLIC_BASE_URL}/mcp` + bearer dev y ver los 7 tools en `/mcp` discovery.
- [ ] **Smoke E2E en rig**: subir un mp3 real de 3 min via Claude Code (tool call directo), ver `transcription_id` retornado, `get_transcription(id)` retorna JSON coherente con `text_content` no vacío.
- [ ] **Smoke E2E cross-user en rig**: el segundo bearer (user B simulado) NO ve la transcription del user A en `list_my_transcriptions()` ni puede `get_transcription(idDeA)` (404).
- [ ] **Smoke FTS**: `search_my_transcriptions(query="palabra-real-del-audio")` retorna match con `rank > 0`.
- [ ] **Legacy endpoint deprecation visible**: `GET /openapi.json` muestra `POST /api/transcriptions` con `"deprecated": true`. Logs del rig contienen al menos un WARN `legacy_endpoint_invoked` post-deploy si algún client lo invocó.
- [ ] **`/health` consistency**: el endpoint sigue devolviendo 200 con todos los campos previos (no regresión Capa 3).
- [ ] **Drifts cerrados**: D-044-impl marcado como cerrado en `docs/sesiones/2026-05-05-wiki-drifts.md` con commit reference.
- [ ] **Multi-agent review** (igual que Capa 2 + Capa 3) sobre el código de Capa 4 antes de mergear `feat/capa4-mcp` a master. Se esperan ≥1 CRITICAL + ≥3 HIGH issues a corregir.
- [ ] **Wiki sync post-merge**: `/graphify --update` corrido para reflejar el módulo `mcp/` nuevo + columna `upload_bearer_hash`. Drift D-010 cerrado.
