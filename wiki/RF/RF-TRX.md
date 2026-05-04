# Módulo TRX — Requerimientos Funcionales (Transcripción y Diarización)

**Source flow**: [`FL-TRX-01`](../FL/FL-TRX-01.md) (revisado en refactor 2.0)
**Architecture**: [`02_arquitectura.md`](../02_arquitectura.md) §3, §4
**Data model**: [`05_modelo_datos.md`](../05_modelo_datos.md)
**Hardening level**: Execution-Normative

> **Nota de versión 2.0**: estos RFs se ejecutan ahora orquestados por la tool MCP `start_transcription` ([RF-MCP-02](RF-MCP.md#rf-mcp-02-tool-start_transcription)), no directamente por HTTP REST. El input no es un upload multipart sino un `upload_id` previamente creado con `request_upload_url` ([RF-MCP-01](RF-MCP.md#rf-mcp-01-tool-request_upload_url)) y consumido por `POST /api/upload` ([RF-MCP-03](RF-MCP.md#rf-mcp-03-endpoint-rest-post-apiupload-audio--image)). Adicionalmente, tanto cache miss como cache hit persisten un registro en `transcriptions` (Postgres) asociado al `user_id` del bearer (ver [ADR-008](../ADR/ADR-008.md)). Los demás aspectos del módulo (lock, validación, errores GPU, persistencia tolerante) siguen vigentes.

## Tabla resumen

| ID | Título | Actor | Pre-condición | Entradas | Salidas | Criterio de aceptación |
|---|---|---|---|---|---|---|
| RF-TRX-01 | Procesar archivo (cache miss) | Cliente Intranet | Lock libre, modelos cargados | Multipart con `file` y opcionales | `200 + TranscriptionResult` | Given audio nuevo, when POST, then JSON diarizado y caché poblado |
| RF-TRX-02 | Devolver resultado cacheado (cache hit) | Cliente Intranet | Existe `<hash>/` con TTL vigente | Multipart con `file` | `200 + TranscriptionResult` cacheado | Given audio idéntico < 24h, when POST, then JSON sin recomputar |
| RF-TRX-03 | Validar formato y tamaño del upload | FastAPI App | Ninguno | Multipart con `file` | `400` o `413` con error_code | Given archivo inválido, when POST, then rechazo tipado |
| RF-TRX-04 | Manejar concurrencia con lock global | FastAPI App | Lock ocupado | Cualquier POST `/transcribe` | `503 + Retry-After` | Given otro request en curso, when POST, then 503 |
| RF-TRX-05 | Manejar errores de GPU | Motor STT/Diar | CUDA OOM o crash | Audio normalizado | `500 + CUDA_OOM` o `MODEL_FAILURE` | Given GPU falla, when procesando, then 500 con stage |
| RF-TRX-06 | Persistencia tolerante a fallos | Caché Filesystem | Disco lleno o permisos | `TranscriptionResult` calculado | `200 + JSON` con log WARN | Given disco lleno, when persist, then response OK + log |

---

## RF-TRX-01: Procesar archivo (cache miss)

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-TRX-01 |
| Título | Procesar archivo de audio con cache miss |
| Actor primario | Cliente Intranet |
| Actor secundario | FastAPI App, Normalizador, WhisperX, pyannote 3.1, Caché Filesystem |
| Prioridad | Alta |
| Severidad | Crítica |
| Flujo origen | FL-TRX-01 §6 |

### Precondiciones detalladas

| # | Condición | Verificación |
|---|---|---|
| 1 | Servicio FastAPI activo | `GET /health` retorna 200 con `gpu_available=true` |
| 2 | Modelos cargados en VRAM | Lifespan completó startup (ver RF-TRX-01 paso 0) |
| 3 | Lock global libre o disponible en ≤ 5 s | `asyncio.Lock` no adquirido |
| 4 | Disco con ≥ 100 MB libres en `<DATA_DIR>/cache/` | `shutil.disk_usage` |
| 5 | Audio del cliente NO existe en caché vigente | Lookup `<hash>/meta.json` retorna ausente o vencido |

### Inputs

| Campo | Tipo | Requerido | Origen | Validación | RN |
|---|---|---|---|---|---|
| `file` | binary (multipart) | Sí | HTTP body | Extensión en `{mp4, mp3, wav, m4a, flac}`, tamaño ≤ `MAX_UPLOAD_MB` | RF-TRX-03 |
| `min_speakers` | integer | No (default `1`) | Form field | `1 ≤ min_speakers ≤ max_speakers ≤ 8` | — |
| `max_speakers` | integer | No (default `8`) | Form field | `min_speakers ≤ max_speakers ≤ 8` | — |
| `language` | string | No (default `"es"`) | Form field | ISO 639-1; warning si distinto de `"es"` | — |

### Process Steps (Happy Path)

| # | Paso | Componente responsable |
|---|---|---|
| 0 | Generar `request_id` (UUID v4) y emitir log `request_received` | FastAPI App (middleware) |
| 1 | Validar formato y tamaño del upload (delegado en RF-TRX-03) | FastAPI App |
| 2 | Adquirir lock global con timeout 5 s (delegado en RF-TRX-04) | FastAPI App |
| 3 | Guardar upload en tempfile (`/tmp/<request_id>/upload.bin`) | FastAPI App |
| 4 | Ejecutar `ffmpeg -i upload.bin -vn -ac 1 -ar 16000 -sample_fmt s16 audio.wav` | Normalizador |
| 5 | Calcular `sha256` del WAV → `audio_hash` (hex 64 chars) | Normalizador |
| 6 | Emitir log `audio_normalized` con `duration_seconds`, `audio_hash`, `duration_ms` | Normalizador |
| 7 | Lookup `<DATA_DIR>/cache/<audio_hash>/meta.json`. Si existe y `now - created_at < ttl_seconds`, ir a RF-TRX-02 | Caché |
| 8 | Emitir log `cache_lookup` con `hit=false` | FastAPI App |
| 9 | Invocar WhisperX large-v3 sobre `audio.wav` con `language="es"`, batch_size configurable | Motor de Transcripción |
| 10 | Emitir log `stt_completed` con `duration_ms`, `num_segments` | FastAPI App |
| 11 | Invocar pyannote 3.1 sobre `audio.wav` con hints `min_speakers`, `max_speakers` | Motor de Diarización |
| 12 | Emitir log `diarize_completed` con `duration_ms`, `num_speakers` | FastAPI App |
| 13 | Llamar `whisperx.assign_word_speakers(diar_result, transcript)` | Ensamblador |
| 14 | Emitir log `merge_completed` con `duration_ms` | FastAPI App |
| 15 | Construir `TranscriptionResult` (ver §05_modelo_datos §2) | FastAPI App |
| 16 | Persistir `<audio_hash>/transcription.json.tmp` y `meta.json.tmp` | Caché Filesystem |
| 17 | `os.rename` ambos archivos a sus nombres finales (atómico) | Caché Filesystem |
| 18 | Emitir log `cache_persisted` con `bytes_written` | Caché Filesystem |
| 19 | INSERT en `transcriptions` (id=transcription_id UUID, user_id, audio_hash, original_filename, original_size_bytes, duration_seconds, language, num_speakers, text, segments JSONB, metadata JSONB) | Postgres |
| 20 | UPDATE upload_sessions SET status='consumed', consumed_at=now() WHERE id=upload_id | Postgres |
| 21 | Borrar `/data/uploads/<upload_id>/` en `finally` | FastAPI App |
| 22 | Liberar lock global | FastAPI App |
| 23 | Emitir log `transcription_persisted(transcription_id, user_id)` y `mcp_request_completed` con `total_duration_ms`, `rtf`, `cache_hit=false` | FastAPI App |
| 24 | Responder al MCP caller con `{transcription_id, status: 'completed', cache_hit: false}` | MCP Server |

### Outputs

| Campo | Tipo | Destino | Efecto observable |
|---|---|---|---|
| HTTP 200 status | int | Cliente | Cliente sabe que el procesamiento fue exitoso. |
| `TranscriptionResult` (JSON) | object | Cliente (HTTP body) | Cliente recibe transcripción diarizada completa. |
| `<DATA_DIR>/cache/<audio_hash>/transcription.json` | file | Filesystem | Próxima request idéntica < 24h será cache hit (RF-TRX-02). |
| `<DATA_DIR>/cache/<audio_hash>/meta.json` | file | Filesystem | Caché conoce el TTL para esta entrada. |
| 9 eventos de log entre `request_received` y `request_completed` | structured logs | stdout JSON | Operador puede inspeccionar trazabilidad. |

### Typed Errors

Ver RF-TRX-03 (validación), RF-TRX-04 (concurrencia), RF-TRX-05 (GPU), RF-TRX-06 (persistencia).

Errores propios de RF-TRX-01:

| Código | Causa | Trigger | Respuesta |
|---|---|---|---|
| `INTERNAL_ERROR` | Excepción no clasificada en orquestación | Cualquier `Exception` no atajada por RF-TRX-03/04/05/06 | HTTP 500 + `{"error_code": "INTERNAL_ERROR", "request_id": "..."}` + log `request_failed` |

### Special Cases and Variants

- **Audio sin habla detectada**: WhisperX retorna `segments=[]`. El sistema responde 200 con `num_speakers=0`, `segments=[]`, y persiste igual.
- **Audio con un único hablante**: pyannote puede retornar 1 speaker; el merge funciona normalmente.
- **`language` distinto a `"es"`**: el sistema procesa, pero loguea WARN `non_spanish_language_used`.
- **Hints `min_speakers > max_speakers`**: rechazo en validación (RF-TRX-03).

### Data Model Impact

- Crea entidad `TranscriptionResult` (ver `05_modelo_datos.md` §2).
- Crea entidad `CacheMeta` (ver `05_modelo_datos.md` §3).
- Estado del request transita: `Recibido → ValidandoFormato → AdquiriendoLock → Normalizando → ConsultandoCache → Transcribiendo → Diarizando → Ensamblando → CacheandoResultado → Respondiendo`.

### Expanded Acceptance Criteria (Gherkin)

```gherkin
Feature: Transcribir y diarizar archivo nuevo

Scenario: Cache miss devuelve transcripción y persiste resultado
  Given el servicio está corriendo con modelos cargados
    And el caché no contiene una entrada vigente para el hash del audio
    And el cliente envía un MP4 válido de 60 minutos en español
  When el cliente hace POST /transcribe con file=reunion.mp4
  Then la respuesta es 200 OK
    And el body cumple el schema TranscriptionResult
    And segments tiene al menos 1 elemento
    And metadata.audio_hash tiene 64 caracteres hexadecimales
    And metadata.cache_hit es false
    And existe el archivo <DATA_DIR>/cache/<audio_hash>/transcription.json
    And existe el archivo <DATA_DIR>/cache/<audio_hash>/meta.json
    And el log estructurado contiene un evento request_completed con cache_hit=false

Scenario: Audio sin habla
  Given un archivo WAV de 30 segundos con solo silencio
    And el caché no contiene una entrada vigente para el hash
  When el cliente hace POST /transcribe con file=silencio.wav
  Then la respuesta es 200 OK
    And segments es []
    And num_speakers es 0
    And existe la entrada de caché para el hash

Scenario Outline: Diferentes formatos de entrada producen el mismo resultado
  Given el mismo contenido de audio en formatos <formato>
    And el caché no contiene la entrada
  When el cliente hace POST /transcribe con file
  Then la respuesta es 200 OK
    And segments[0].text es <texto_esperado>

  Examples:
    | formato | texto_esperado |
    | mp4     | "Buenas, gracias por venir." |
    | mp3     | "Buenas, gracias por venir." |
    | wav     | "Buenas, gracias por venir." |
```

### Test Traceability

| Test ID | Tipo | Cubre |
|---|---|---|
| TP-TRX-01-pos-01 | Positivo (E2E) | MP4 de 60 min retorna 200 con TranscriptionResult válido |
| TP-TRX-01-pos-02 | Positivo (E2E) | Audio sin habla retorna 200 con `segments=[]` |
| TP-TRX-01-pos-03 | Positivo (parametric) | MP4, MP3, WAV con mismo audio dan mismo resultado |
| TP-TRX-01-neg-01 | Negativo (E2E) | Excepción interna no atajada → 500 INTERNAL_ERROR |
| TP-TRX-01-cov-01 | Cobertura | Cada paso de §Process Steps emite su log esperado |

### No Ambiguities Left

- **Forbidden assumptions**: no se asume que el cliente tenga el JSON cacheado en su cliente; cada POST se procesa en servidor.
- **Closed decisions**: stack STT (ADR-001), diarización (ADR-002), API síncrona (ADR-003), caché filesystem (ADR-004), lock global (ADR-005), hash key (ADR-007).
- **Out of scope**: live transcription, generación de minutas, persistencia >24h.
- **External deps**: HuggingFace Hub (one-time descarga de modelos en startup), ffmpeg binario presente en `$PATH`, GPU NVIDIA con drivers compatibles.

**TODO explicit = 0**.

---

## RF-TRX-02: Devolver resultado cacheado (cache hit)

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-TRX-02 |
| Título | Devolver resultado cacheado para audio idéntico dentro del TTL |
| Actor primario | Cliente Intranet |
| Actor secundario | FastAPI App, Normalizador, Caché Filesystem |
| Prioridad | Alta |
| Severidad | Mayor |
| Flujo origen | FL-TRX-01 §7 |

### Precondiciones detalladas

| # | Condición | Verificación |
|---|---|---|
| 1 | Servicio FastAPI activo | `GET /health` retorna 200 |
| 2 | Existe `<DATA_DIR>/cache/<audio_hash>/meta.json` | `os.path.exists` |
| 3 | Entrada de caché vigente | `now_utc() - parse_iso(meta.created_at) < timedelta(seconds=meta.ttl_seconds)` |
| 4 | `meta.schema_version` igual a la versión actual del schema | `meta.schema_version == 1` |
| 5 | `<audio_hash>/transcription.json` existe y es parseable | JSON load no lanza excepción |

### Inputs

Idénticos a RF-TRX-01.

### Process Steps (Happy Path)

| # | Paso | Componente responsable |
|---|---|---|
| 0 | Generar `request_id`, emitir `request_received` | FastAPI App |
| 1 | Validar formato y tamaño (RF-TRX-03) | FastAPI App |
| 2 | Adquirir lock global (RF-TRX-04) | FastAPI App |
| 3 | Guardar upload en tempfile | FastAPI App |
| 4 | Normalizar y hashear (mismos pasos que RF-TRX-01 4-6) | Normalizador |
| 5 | Lookup `<audio_hash>/meta.json` y verificar TTL | Caché |
| 6 | Si hit: leer `transcription.json` y parsearlo | Caché Filesystem |
| 7 | Emitir log `cache_lookup` con `hit=true` | FastAPI App |
| 8 | INSERT en `transcriptions` (mismo schema que cache miss; user_id del bearer, mismos segments JSONB cacheados, metadata.served_from_cache=true) | Postgres |
| 9 | UPDATE upload_sessions SET status='consumed', consumed_at=now() | Postgres |
| 10 | Borrar tempfiles en `finally` | FastAPI App |
| 11 | Liberar lock | FastAPI App |
| 12 | Emitir log `transcription_persisted` y `mcp_request_completed` con `cache_hit=true` y `total_duration_ms` (típicamente <10s) | FastAPI App |
| 13 | Responder al MCP caller con `{transcription_id, status: 'completed', cache_hit: true}` | MCP Server |

### Outputs

| Campo | Tipo | Destino | Efecto observable |
|---|---|---|---|
| HTTP 200 status | int | Cliente | OK |
| `TranscriptionResult` (cached) | object | Cliente | Idéntico al que se devolvió la primera vez. |
| `metadata.served_from_cache` | bool | Cliente (HTTP body) | `true` para diferenciar hit de miss. |
| Caché no se modifica | — | Filesystem | `created_at` original se preserva. |

### Typed Errors

| Código | Causa | Trigger | Respuesta |
|---|---|---|---|
| `INTERNAL_ERROR` | `transcription.json` corrupto | JSON parse falla | HTTP 500 + log; el operador debe purgar la entrada manualmente o el cleanup la considera huérfana |

### Special Cases and Variants

- **Schema version mismatch**: si `meta.schema_version != schema_version_actual`, se trata como cache miss y se reprocesa (entrada vieja se sobreescribe).
- **`meta.json` legible pero `transcription.json` corrupto**: retorna `INTERNAL_ERROR` y deja la entrada para que el cleanup la purgue.

### Data Model Impact

- Lectura de `TranscriptionResult` y `CacheMeta`. No hay escritura.
- Estado del request: `Recibido → ValidandoFormato → AdquiriendoLock → Normalizando → ConsultandoCache → RespondiendoDesdeCache`.

### Expanded Acceptance Criteria (Gherkin)

```gherkin
Scenario: Cache hit devuelve resultado en menos de 10 segundos
  Given el caché contiene una entrada vigente para el hash del audio
    And el cliente envía el mismo MP4 que generó esa entrada
  When el cliente hace POST /transcribe con file=reunion.mp4
  Then la respuesta es 200 OK
    And el total_duration_ms del log es menor a 10000
    And metadata.served_from_cache es true
    And el contenido de segments es idéntico al de la entrada cacheada
    And el log contiene cache_lookup con hit=true

Scenario: Cache hit no modifica created_at de la entrada
  Given el caché tiene una entrada con created_at = "2026-04-30T10:00:00Z"
    And la hora actual es "2026-04-30T20:00:00Z"
  When el cliente hace POST /transcribe del mismo audio
  Then la respuesta es 200
    And el archivo meta.json sigue teniendo created_at = "2026-04-30T10:00:00Z"

Scenario: Cache vencido se trata como miss
  Given el caché tiene una entrada con created_at de hace 25 horas y ttl_seconds=86400
  When el cliente hace POST /transcribe del mismo audio
  Then se ejecuta el flujo completo (RF-TRX-01)
    And se sobreescribe la entrada con created_at = ahora
```

### Test Traceability

| Test ID | Tipo | Cubre |
|---|---|---|
| TP-TRX-02-pos-01 | Positivo (E2E) | Mismo audio dentro de TTL retorna 200 < 10 s con `served_from_cache=true` |
| TP-TRX-02-pos-02 | Positivo (E2E) | Audio en distinto formato (MP3 vs MP4 con mismo audio) hace cache hit |
| TP-TRX-02-pos-03 | Positivo | Cache hit no modifica `created_at` |
| TP-TRX-02-neg-01 | Negativo | Entrada vencida → cache miss |
| TP-TRX-02-neg-02 | Negativo | `transcription.json` corrupto → 500 INTERNAL_ERROR |
| TP-TRX-02-neg-03 | Negativo | `schema_version` desactualizado → cache miss y sobreescritura |

### No Ambiguities Left

- **Forbidden assumptions**: el caché no se invalida por cambios de modelo; si el operador actualiza Whisper, debe purgar manualmente o esperar TTL.
- **Closed decisions**: clave por hash del audio normalizado (ADR-007), TTL 24 h (ADR-004).
- **Out of scope**: invalidación inteligente por cambio de modelo, refresh manual de TTL.
- **External deps**: ninguno adicional a RF-TRX-01.

**TODO explicit = 0**.

---

## RF-TRX-03: Validar formato y tamaño del upload

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-TRX-03 |
| Título | Validar formato, extensión y tamaño del archivo subido |
| Actor primario | FastAPI App |
| Prioridad | Alta |
| Severidad | Mayor |
| Flujo origen | FL-TRX-01 §8 fila 1, 2 |

### Precondiciones detalladas

| # | Condición | Verificación |
|---|---|---|
| 1 | Variable `MAX_UPLOAD_MB` configurada (default `500`) | env var presente |
| 2 | Lista de extensiones permitidas configurada (default `mp4,mp3,wav,m4a,flac`) | env var o constante |

### Inputs

| Campo | Tipo | Requerido | Validación |
|---|---|---|---|
| `file` | binary multipart | Sí | Header `content-length` ≤ `MAX_UPLOAD_MB * 1024 * 1024` |
| Filename del file | string | Sí | Extensión (post último `.`) en lista permitida (case insensitive) |

### Process Steps (Happy Path)

| # | Paso | Componente responsable |
|---|---|---|
| 1 | FastAPI middleware verifica `content-length` ≤ `MAX_UPLOAD_MB * 1024 * 1024` | FastAPI |
| 2 | Si excede: responder 413 + `{"error_code": "FILE_TOO_LARGE", ...}` | FastAPI |
| 3 | Extraer extensión del filename: `Path(filename).suffix.lower().lstrip(".")` | Handler |
| 4 | Si extensión no está en `ALLOWED_EXTENSIONS`: responder 400 + `{"error_code": "UNSUPPORTED_EXTENSION", "detail": "Allowed: mp4, mp3, wav, m4a, flac"}` | Handler |
| 5 | Verificar que el contenido no sea vacío (`len(bytes) > 0`) | Handler |
| 6 | Si vacío: responder 400 + `{"error_code": "INVALID_FORMAT", "detail": "Empty file"}` | Handler |
| 7 | Devolver control a RF-TRX-01 paso 2 | Handler |

### Outputs

| Campo | Tipo | Destino | Efecto observable |
|---|---|---|---|
| Validación pasa | — | Continúa pipeline | RF-TRX-01 sigue paso 2 |
| HTTP 400/413 con error_code | JSON | Cliente | Cliente sabe el motivo del rechazo |

### Typed Errors

| Código | HTTP | Causa | Trigger |
|---|---|---|---|
| `FILE_TOO_LARGE` | 413 | Archivo excede límite | `content-length > MAX_UPLOAD_MB*1024*1024` |
| `UNSUPPORTED_EXTENSION` | 400 | Extensión no permitida | `extension not in ALLOWED_EXTENSIONS` |
| `INVALID_FORMAT` | 400 | Contenido vacío o corrupto antes de ffmpeg | `len(file.read()) == 0` |

### Special Cases and Variants

- **Filename sin extensión** (ej: `audio` sin sufijo): rechazo con `UNSUPPORTED_EXTENSION`.
- **Doble extensión** (ej: `audio.tar.mp3`): se evalúa solo la última (`.mp3`).
- **Extensión correcta pero contenido falso** (ej: `.mp4` con bytes que no son MP4): pasa esta validación; ffmpeg falla en RF-TRX-01 paso 4 y se responde con `INVALID_FORMAT`.
- **`min_speakers > max_speakers`**: 400 + `{"error_code": "INVALID_PARAMETER", "detail": "min_speakers must be <= max_speakers"}`.

### Data Model Impact

Ninguno. Validación pre-pipeline.

### Expanded Acceptance Criteria (Gherkin)

```gherkin
Scenario: Archivo de tamaño aceptable y extensión permitida
  Given MAX_UPLOAD_MB=500
    And el cliente envía un MP4 de 100 MB
  When POST /transcribe
  Then la validación pasa al siguiente paso

Scenario Outline: Rechazo por tamaño o extensión
  Given MAX_UPLOAD_MB=500
  When POST /transcribe con archivo <descripcion>
  Then la respuesta es <status>
    And el body contiene error_code=<error_code>

  Examples:
    | descripcion                       | status | error_code            |
    | MP4 de 600 MB                     | 413    | FILE_TOO_LARGE        |
    | archivo .txt de 1 MB              | 400    | UNSUPPORTED_EXTENSION |
    | archivo sin extensión             | 400    | UNSUPPORTED_EXTENSION |
    | archivo .mp4 de 0 bytes           | 400    | INVALID_FORMAT        |

Scenario: Parámetro min_speakers > max_speakers
  When POST /transcribe con file válido, min_speakers=8, max_speakers=2
  Then la respuesta es 400
    And error_code es INVALID_PARAMETER
```

### Test Traceability

| Test ID | Tipo | Cubre |
|---|---|---|
| TP-TRX-03-pos-01 | Positivo | MP4 de 100 MB con extensión válida pasa |
| TP-TRX-03-neg-01 | Negativo | Archivo de 600 MB → 413 FILE_TOO_LARGE |
| TP-TRX-03-neg-02 | Negativo | `.txt` → 400 UNSUPPORTED_EXTENSION |
| TP-TRX-03-neg-03 | Negativo | Sin extensión → 400 UNSUPPORTED_EXTENSION |
| TP-TRX-03-neg-04 | Negativo | 0 bytes → 400 INVALID_FORMAT |
| TP-TRX-03-neg-05 | Negativo | `min_speakers > max_speakers` → 400 INVALID_PARAMETER |

### No Ambiguities Left

- **Forbidden assumptions**: no se confía en `Content-Type` HTTP; sólo en extensión + intento de ffmpeg.
- **Closed decisions**: lista de extensiones es config (default `mp4,mp3,wav,m4a,flac`); no se incluyen `.ogg` ni `.opus` por baja prevalencia (revisar si Sandinas lo usa).
- **Out of scope**: validación profunda del contenido (magic bytes); se delega a ffmpeg.

**TODO explicit = 0**.

---

## RF-TRX-04: Manejar concurrencia con lock global

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-TRX-04 |
| Título | Lock global para garantizar 1 procesamiento simultáneo |
| Actor primario | FastAPI App |
| Prioridad | Alta |
| Severidad | Crítica |
| Flujo origen | FL-TRX-01 §8 fila 3 |

### Precondiciones detalladas

| # | Condición | Verificación |
|---|---|---|
| 1 | Existe instancia única de `asyncio.Lock` en el proceso | Lifespan inicializa `app.state.transcription_lock` |
| 2 | Variable `LOCK_WAIT_SECONDS` configurada (default `5.0`) | env var |
| 3 | Variable `LOCK_RETRY_AFTER_SECONDS` configurada (default `600`) | env var |

### Inputs

Sin inputs propios; opera sobre cualquier POST `/transcribe`.

### Process Steps (Happy Path)

| # | Paso | Componente responsable |
|---|---|---|
| 1 | Intentar `await asyncio.wait_for(lock.acquire(), timeout=LOCK_WAIT_SECONDS)` | Handler |
| 2 | Si adquiere: continuar con RF-TRX-01 paso 3 | Handler |
| 3 | En `finally` del handler: `lock.release()` (idempotente; no falla si no se adquirió) | Handler |
| 4 | Si `asyncio.TimeoutError`: emitir log `lock_busy`, responder 503 + headers `Retry-After: <LOCK_RETRY_AFTER_SECONDS>` + body `{"error_code": "LOCK_BUSY", "retry_after_seconds": <N>, "request_id": "..."}` | Handler |

### Outputs

| Campo | Tipo | Destino | Efecto observable |
|---|---|---|---|
| Lock adquirido | — | RF-TRX-01 continúa | El otro request espera o recibe 503 |
| HTTP 503 + Retry-After | int + header | Cliente concurrente | Cliente puede reintentar tras ese tiempo |
| Log `lock_busy` | log | stdout JSON | Operador ve frecuencia de contención |

### Typed Errors

| Código | HTTP | Causa | Trigger |
|---|---|---|---|
| `LOCK_BUSY` | 503 | Lock global ocupado más de `LOCK_WAIT_SECONDS` | `asyncio.TimeoutError` al hacer `wait_for` |

### Special Cases and Variants

- **Cliente desconecta antes de adquirir lock**: el `wait_for` se cancela; lock no se adquiere; ningún side effect.
- **Excepción dentro del lock**: el `finally` libera. La excepción se propaga al manejador de RF-TRX-05.
- **Crash del proceso con lock adquirido**: el lock muere con el proceso; no hay leak persistente (en memoria).

### Data Model Impact

Ninguno.

### Expanded Acceptance Criteria (Gherkin)

```gherkin
Scenario: Segundo request mientras hay uno en curso
  Given un request A está en proceso (sosteniendo el lock)
  When el cliente B hace POST /transcribe inmediatamente
  Then después de 5 segundos B recibe 503
    And la respuesta tiene header Retry-After: 600
    And el body tiene error_code=LOCK_BUSY
    And el log contiene un evento lock_busy con el request_id de B

Scenario: Segundo request espera y obtiene el lock
  Given un request A termina en menos de 5 segundos
  When el cliente B hace POST /transcribe mientras A está activo
  Then B espera y luego procesa normalmente

Scenario: Lock se libera ante excepción
  Given un request A entra al lock
    And ocurre una excepción interna durante el procesamiento
  When termina el handler de A
  Then el lock está liberado
    And un request B siguiente puede adquirirlo
```

### Test Traceability

| Test ID | Tipo | Cubre |
|---|---|---|
| TP-TRX-04-pos-01 | Positivo | 2 requests serializados completan correctamente |
| TP-TRX-04-pos-02 | Positivo | Lock se libera tras éxito |
| TP-TRX-04-pos-03 | Positivo | Lock se libera tras excepción |
| TP-TRX-04-neg-01 | Negativo | 2 requests simultáneos: el segundo recibe 503 con `Retry-After: 600` |
| TP-TRX-04-neg-02 | Negativo | Cliente desconectado antes de adquirir → ningún side effect |

### No Ambiguities Left

- **Forbidden assumptions**: no se asume que `Retry-After` lo respete el cliente; es informativo.
- **Closed decisions**: timeout de espera 5 s, retry-after 600 s; ambos configurables. ADR-005.
- **Out of scope**: cola de requests pending; queueing distribuido.

**TODO explicit = 0**.

---

## RF-TRX-05: Manejar errores de GPU

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-TRX-05 |
| Título | Recuperarse de errores de CUDA y modelo durante procesamiento |
| Actor primario | Motor de Transcripción y Diarización |
| Prioridad | Alta |
| Severidad | Crítica |
| Flujo origen | FL-TRX-01 §8 fila 4 |

### Precondiciones detalladas

| # | Condición | Verificación |
|---|---|---|
| 1 | Lock global adquirido y request en `Transcribiendo` o `Diarizando` | Estado del request |
| 2 | Modelos cargados; GPU disponible al inicio del request | Lifespan completó |

### Inputs

Audio normalizado WAV en tempfile.

### Process Steps (Happy Path)

| # | Paso | Componente responsable |
|---|---|---|
| 1 | Wrap llamada a WhisperX en `try/except` capturando `torch.cuda.OutOfMemoryError` y `RuntimeError` con mensaje "CUDA out of memory" | Handler |
| 2 | Wrap llamada a pyannote en `try/except` similar | Handler |
| 3 | Si `torch.cuda.OutOfMemoryError`: ejecutar `torch.cuda.empty_cache()`, emitir log ERROR `request_failed` con `error_code=CUDA_OOM`, `stage="stt"` o `"diarize"` | Handler |
| 4 | Si excepción no clasificada del modelo: emitir log ERROR con `error_code=MODEL_FAILURE`, `stage`, `exception_class` | Handler |
| 5 | Liberar lock global y borrar tempfiles en `finally` | Handler |
| 6 | Responder HTTP 500 + `{"error_code": "CUDA_OOM" o "MODEL_FAILURE", "stage": "...", "request_id": "..."}` | Handler |

### Outputs

| Campo | Tipo | Destino | Efecto observable |
|---|---|---|---|
| HTTP 500 + body tipado | JSON | Cliente | Cliente sabe el motivo y la etapa |
| Lock liberado | — | Sistema | Próximos requests pueden adquirirlo |
| Tempfiles borrados | — | Filesystem | No hay leak |
| Log `request_failed` ERROR | log | stdout | Operador ve frecuencia y stage |

### Typed Errors

| Código | HTTP | Causa | Trigger |
|---|---|---|---|
| `CUDA_OOM` | 500 | GPU sin memoria | `torch.cuda.OutOfMemoryError` o RuntimeError CUDA OOM |
| `MODEL_FAILURE` | 500 | Crash del modelo no clasificado | Cualquier excepción en `transcribe()` o `diarize()` no clasificada |

### Special Cases and Variants

- **OOM repetido**: si dos requests consecutivos producen OOM, el operador debe revisar (probable VRAM con basura). Mitigación: alerta basada en log si `CUDA_OOM` ocurre 2+ veces en 1h.
- **Modelo no carga al startup**: lifespan falla; el contenedor reinicia (Docker healthcheck).

### Data Model Impact

- Estado del request transita a `ErrorGPU`. No se persiste caché.

### Expanded Acceptance Criteria (Gherkin)

```gherkin
Scenario: CUDA OOM durante transcripción
  Given el modelo Whisper está cargado
    And se inyecta un mock que lanza torch.cuda.OutOfMemoryError
  When POST /transcribe con file válido
  Then la respuesta es 500
    And error_code es CUDA_OOM
    And stage es "stt"
    And el lock está liberado
    And no se persiste caché

Scenario: Crash inesperado en pyannote
  Given se inyecta un mock que lanza RuntimeError("pyannote internal")
  When POST /transcribe con file válido
  Then la respuesta es 500
    And error_code es MODEL_FAILURE
    And stage es "diarize"
    And el log contiene exception_class="RuntimeError"

Scenario: Recuperación tras OOM
  Given un request A produjo CUDA_OOM
  When un cliente B hace POST /transcribe inmediatamente
  Then B procesa normalmente (lock disponible, VRAM liberada)
```

### Test Traceability

| Test ID | Tipo | Cubre |
|---|---|---|
| TP-TRX-05-neg-01 | Negativo (mock) | OOM en STT → 500 CUDA_OOM stage=stt |
| TP-TRX-05-neg-02 | Negativo (mock) | OOM en diarización → 500 CUDA_OOM stage=diarize |
| TP-TRX-05-neg-03 | Negativo (mock) | Excepción genérica en STT → 500 MODEL_FAILURE |
| TP-TRX-05-pos-01 | Positivo | Tras error, próximo request funciona (lock liberado) |

### No Ambiguities Left

- **Forbidden assumptions**: no se reintenta automáticamente; el cliente decide.
- **Closed decisions**: `empty_cache()` se llama tras OOM; no se fuerza reload del modelo.
- **Out of scope**: degradación a modelo más chico (large-v3 → large-v2); reinicio automático del contenedor.

**TODO explicit = 0**.

---

## RF-TRX-06: Persistencia tolerante a fallos de disco

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-TRX-06 |
| Título | Tolerar fallos al persistir el caché sin afectar la respuesta al cliente |
| Actor primario | Caché Filesystem |
| Prioridad | Media |
| Severidad | Mayor |
| Flujo origen | FL-TRX-01 §8 fila 6 |

### Precondiciones detalladas

| # | Condición | Verificación |
|---|---|---|
| 1 | `TranscriptionResult` ya calculado (paso 15 de RF-TRX-01) | Variable en memoria |

### Inputs

`TranscriptionResult`, `audio_hash`, `original_filename`, `original_size_bytes`, `duration_seconds`.

### Process Steps (Happy Path)

| # | Paso | Componente responsable |
|---|---|---|
| 1 | Intentar `os.makedirs(<DATA_DIR>/cache/<audio_hash>/, exist_ok=True)` | Caché |
| 2 | Escribir `transcription.json.tmp` y `meta.json.tmp` | Caché |
| 3 | `os.rename` ambos a su nombre final (atómico) | Caché |
| 4 | Emitir log `cache_persisted` con `bytes_written` | Caché |
| 5 | Si cualquier paso 1-3 falla con `OSError` (disk full, permission denied, etc.): emitir log WARN `cache_persist_failed` con `error_code` y `audio_hash`; **continuar normalmente** | Caché |
| 6 | El handler de RF-TRX-01 sigue respondiendo 200 al cliente con el JSON ya calculado | Handler |

### Outputs

| Campo | Tipo | Destino | Efecto observable |
|---|---|---|---|
| Caché poblado (caso normal) | files | Filesystem | Próxima request idéntica = cache hit |
| Caché vacío + log WARN (caso de fallo) | log | stdout | Cliente recibe igual el JSON; próxima request será cache miss |

### Typed Errors

No genera errores HTTP; sólo logs WARN. Ver §Special Cases.

### Special Cases and Variants

- **Disco lleno**: `OSError [Errno 28] No space left on device` → log WARN `cache_persist_failed`. El operador debe purgar disco o ajustar TTL/cleanup.
- **Permission denied**: `PermissionError` → log WARN. Requiere fix manual del operador.
- **Crash a mitad de escritura**: el rename atómico garantiza que nunca queden archivos `.json` parciales en estado final. Los `.tmp` huérfanos se ignoran (no rompen lookup) y se purgan en el siguiente cleanup como huérfanos.

### Data Model Impact

- En caso normal: crea `TranscriptionResult` y `CacheMeta` en `<DATA_DIR>/cache/<audio_hash>/`.
- En caso de fallo: estado del caché no cambia.

### Expanded Acceptance Criteria (Gherkin)

```gherkin
Scenario: Persistencia exitosa (caso normal)
  Given el TranscriptionResult está calculado
    And hay 100 MB libres en el disco
  When se ejecuta la persistencia
  Then existen los archivos transcription.json y meta.json en <hash>/
    And el log contiene cache_persisted con bytes_written

Scenario: Disco lleno no afecta la respuesta
  Given el TranscriptionResult está calculado
    And se inyecta un mock que hace que open() lance OSError errno 28
  When se ejecuta la persistencia
  Then el log contiene cache_persist_failed con error_code=DISK_FULL
    And el handler responde 200 con el TranscriptionResult al cliente
    And no hay archivos parciales en <hash>/

Scenario: Crash a mitad de escritura
  Given se inyecta un crash entre escribir transcription.json.tmp y rename
  When se ejecuta la persistencia
  Then no existe transcription.json (sólo .tmp huérfano)
    And el lookup posterior lo trata como cache miss
```

### Test Traceability

| Test ID | Tipo | Cubre |
|---|---|---|
| TP-TRX-06-pos-01 | Positivo | Persistencia normal genera ambos archivos |
| TP-TRX-06-neg-01 | Negativo (mock) | OSError 28 → log WARN, 200 al cliente |
| TP-TRX-06-neg-02 | Negativo (mock) | PermissionError → log WARN, 200 al cliente |
| TP-TRX-06-neg-03 | Negativo (simulado) | Crash entre tmp y rename no deja archivos finales |

### No Ambiguities Left

- **Forbidden assumptions**: no se asume que el operador monitoree disk usage; el log WARN es la única señal.
- **Closed decisions**: ante fallo, devolver al cliente; no propagar como error HTTP. ADR-004.
- **Out of scope**: alertas activas (PagerDuty, Slack); reintento automático.

**TODO explicit = 0**.
