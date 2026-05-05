# SPEC-capa3-pipeline-v1

> **Capa 3 — Audio pipeline (transcripción + diarización) sobre REST con bearer auth**
>
> **Fecha**: 2026-05-05
> **Source RFs**: RF-TRX-01..06, RF-CACHE-01..04, parte de RF-MCP-03 (REST upload sin MCP wrapper).
> **Status**: Aprobado (defaults cerrados por Franco, instrucción "vos elegís").
> **Hardening level**: Execution-Normative. TODO explicit = 0.

---

## 0. Alcance y dependencias

### 0.1 Lo que entra

- Endpoint REST autenticado por bearer (`Authorization: Bearer <plaintext>` validado por `get_current_user_mcp` de Capa 2) que recibe un archivo de audio/video, corre el pipeline Whisper + pyannote en GPU, y devuelve la transcripción diarizada en JSON.
- Cache filesystem por `audio_hash` (SHA-256 del input ya normalizado) con TTL 24 h.
- Lock global sobre la GPU para serializar pipelines (ADR-005: el rig tiene UNA RTX 4060 Ti, no entran dos jobs simultáneos en VRAM).
- Cleanup job en startup: purga entradas de cache vencidas y `upload_sessions` huérfanas (no creadas en Capa 3 todavía pero la tabla ya existe de Capa 1).
- Persistencia en Postgres: row en `transcriptions` con `user_id = bearer.user_id` (scoping ADR-014/015 fail-closed se encarga de filtrar consultas posteriores).
- `[pipeline]` extras instalados en la imagen Docker: torch, torchaudio, whisperx, pyannote.audio, ffmpeg-python.

### 0.2 Lo que NO entra (deferido a Capa 4+)

- MCP server (tools `request_upload_url`, `start_transcription`, `list/search/get_transcription`).
- Two-step upload pattern (`request_upload_url` → upload → `start_transcription`). Capa 3 hace todo en una sola request multipart por simplicidad MVP.
- Soft delete + `delete_transcription` tool (RF-MCP-09).
- Imágenes adjuntas (Capa 5 — IMG module).
- UI (Capa 5).

### 0.3 Decisiones cerradas (defaults Franco)

| Decisión | Valor | Justificación |
|---|---|---|
| Single transcription o input chunking | **Single, sin chunking del input** | WhisperX hace chunks internos de 30 s. Reuniones >1 h son raras en Sandinas; cuando aparezcan, escalamos. |
| Async vs sync API | **Sync con timeout `PIPELINE_TIMEOUT_SECONDS=1800`** | ADR-003 vigente. El client (futuro MCP) sostiene la conexión. |
| WAV intermedio: borrar o cachear | **Borrar tras pipeline** | El cache es por `audio_hash` del input original (mp4/mp3/m4a). El WAV regenerable a costo bajo. Privacy > Performance. |
| WhisperX `batch_size` | **8 con `int8_float16`** | Default documentado para 8 GB VRAM. Si OOM bajo carga real, bajar a 4 (ajustable via env `WHISPER_BATCH_SIZE`). |

### 0.4 Drifts vs wiki (loguear durante implementación)

- **D-026 (futuro)**: la wiki asume MCP tools como entry point para el pipeline (RF-MCP-02 `start_transcription`); Capa 3 expone REST directo `POST /api/transcriptions` para desbloquear desarrollo sin MCP. Capa 4 envuelve el mismo orchestrator con MCP tools sin cambiar la lógica.

---

## 1. Endpoints

### 1.1 `POST /api/transcriptions`

Auth: bearer (`Authorization: Bearer <plaintext>`). Valida con `get_current_user_mcp`. 401 si falta o inválido.

**Input** (multipart/form-data):

| Campo | Tipo | Obligatorio | Descripción |
|---|---|---|---|
| `file` | UploadFile (binary) | Sí | mp4, mp3, m4a, wav, flac. Validado por extensión + magic bytes. |
| `language` | str | No (default "es") | ISO 639-1. Pasado a WhisperX. |
| `num_speakers` | int | No (default null) | Hint a pyannote. Si null, autodetecta. |
| `min_speakers` | int | No (default null) | Lower bound. |
| `max_speakers` | int | No (default null) | Upper bound. |

**Output** (JSON, 200 OK):

```json
{
  "transcription_id": "uuid",
  "audio_hash": "sha256-hex",
  "language": "es",
  "duration_seconds": 312.45,
  "num_speakers": 3,
  "text_content": "SPEAKER_01: Buenos días...\nSPEAKER_02: Hola...",
  "segments": [
    {
      "start": 0.0,
      "end": 5.32,
      "speaker": "SPEAKER_01",
      "text": "Buenos días.",
      "words": [
        {"word": "Buenos", "start": 0.0, "end": 0.45, "speaker": "SPEAKER_01"},
        {"word": "días", "start": 0.46, "end": 1.05, "speaker": "SPEAKER_01"}
      ]
    }
  ],
  "metadata": {
    "model": "whisper-large-v3",
    "compute_type": "int8_float16",
    "diarizer": "pyannote-3.1",
    "cache_hit": false,
    "processing_seconds": 45.2
  }
}
```

### 1.2 `GET /api/transcriptions/{id}`

Auth: bearer. Retorna el JSON cacheado de la transcripción si pertenece al user del bearer (ADR-014/015 fail-closed garantiza el scoping). 404 si no existe o pertenece a otro user (mismo response shape para evitar leak de existence).

### 1.3 `GET /health` (extensión, no nuevo endpoint)

Capa 3 extiende `/health` con campos:

```json
{
  ...campos previos,
  "models": {
    "whisper": "ready" | "loading" | "error",
    "pyannote": "ready" | "loading" | "error",
    "vram_used_mb": 7234
  },
  "pipeline": {
    "lock_held": false,
    "active_job_id": null,
    "queue_depth": 0
  }
}
```

---

## 2. Main Flow (cache miss path)

```
Cliente (curl/MCP-futuro)
  │
  │ POST /api/transcriptions   Bearer + multipart(file=meeting.mp4)
  ▼
get_current_user_mcp (Capa 2) → arma db.info["user_id"], retorna User
  │
  ▼
Validate file: extension + magic bytes + size ≤ MAX_UPLOAD_MB
  │  fail → 413 AUDIO_TOO_LARGE / 400 AUDIO_FORMAT_INVALID
  ▼
Save raw upload to /data/uploads/<random-id>.<ext>
  │
  ▼
Acquire pipeline lock (asyncio.Lock with LOCK_WAIT_SECONDS timeout)
  │  timeout → 503 GPU_BUSY + Retry-After: LOCK_RETRY_AFTER_SECONDS
  ▼
ffmpeg normalize → /data/uploads/<random-id>.normalized.wav (16 kHz mono)
  │  fail → 500 PIPELINE_NORMALIZE_ERROR
  ▼
Compute audio_hash = SHA-256(normalized_wav_bytes)
  │
  ▼
Cache lookup at /data/cache/<audio_hash>/result.json
  ├── HIT: read JSON → SKIP pipeline → INSERT row in transcriptions → return
  │
  └── MISS:
       ▼
     Whisper transcribe (batch_size=WHISPER_BATCH_SIZE=8)
       │  OOM/CUDA → 500 GPU_ERROR (release lock!)
       ▼
     pyannote diarize (with optional num_speakers hint)
       │  fail → 500 PIPELINE_DIARIZE_ERROR
       ▼
     Merge: assign each word to a speaker → segments
       ▼
     Persist:
       1. /data/cache/<audio_hash>/result.json (filesystem cache)
       2. INSERT into transcriptions (Postgres, user_id armado)
  ▼
Release lock
  │
  ▼
Cleanup raw upload + normalized WAV (always, finally block)
  │
  ▼
200 + JSON
```

### 2.1 Postcondiciones del happy path

- `transcriptions` tiene 1 row nuevo con `user_id = current_user.id`.
- `/data/cache/<audio_hash>/result.json` existe y es legible.
- `/data/uploads/<random-id>.*` ya no existe (cleanup en `finally`).
- Lock liberado.
- `last_used_at` del bearer bumped (Capa 2 best-effort).

---

## 3. Acceptance Criteria

| ID | Criterio | RF |
|---|---|---|
| AC-1 | Given bearer válido + mp3 nuevo, When POST `/api/transcriptions`, Then 200 + JSON con `transcription_id`, `segments`, `audio_hash`. Row en `transcriptions` con `user_id = bearer.user_id`. Cache filesystem creado. | RF-TRX-01 |
| AC-2 | Given bearer válido + mp3 idéntico subido recientemente, When POST `/api/transcriptions`, Then 200 + JSON marcado `cache_hit: true`, NO se invocó Whisper ni pyannote (assertion vía mock o latencia <500 ms). Row nuevo en `transcriptions` igual (cada user mantiene su histórico). | RF-TRX-02 |
| AC-3 | Given sin Authorization header, When POST, Then 401 `AUTH_NOT_AUTHENTICATED`. | RF-AUTH-* (Capa 2) |
| AC-4 | Given file con extensión `.exe`, When POST, Then 400 `AUDIO_FORMAT_INVALID`. | RF-TRX-03 |
| AC-5 | Given file > MAX_UPLOAD_MB, When POST, Then 413 `AUDIO_TOO_LARGE` ANTES de ffmpeg (chequeo de Content-Length). | RF-TRX-03 |
| AC-6 | Given dos POST concurrentes (mismo o distinto user), When ambos arriban, Then el primero procesa, el segundo recibe 503 `GPU_BUSY` con `Retry-After: 600`. | RF-TRX-04 |
| AC-7 | Given Whisper falla con CUDA OOM, When pipeline, Then 500 `GPU_ERROR` Y el lock se libera Y la DB queda sin row parcial Y el upload temporal se borra. | RF-TRX-05 |
| AC-8 | Given user A sube file, user B con su propio bearer hace `GET /api/transcriptions/{id}` con id de A, Then 404 (no leak de existence). | ADR-014/015 + AC-18 Capa 2 |
| AC-9 | Given lifespan termina de cargar modelos, When `/health`, Then `models.whisper = "ready"` y `models.pyannote = "ready"`. Antes de eso `/health` devuelve `loading` y `POST /api/transcriptions` retorna 503 `MODELS_NOT_LOADED`. | RF-TRX-06 (init) |
| AC-10 | Given cleanup job corre, When entrada en cache > CACHE_TTL_SECONDS, Then se borra. | RF-CACHE-02 |
| AC-11 | Given pipeline tarda > PIPELINE_TIMEOUT_SECONDS, When timeout, Then 504 `PIPELINE_TIMEOUT`, lock liberado, NO se persiste. | RF-TRX-* |
| AC-12 | Given audio_hash conocido, When dos users distintos lo suben separadamente, Then el cache filesystem se reusa (1 sola corrida de pipeline) Y cada user tiene su propio row en `transcriptions` (privacy preservada porque el contenido del cache es el resultado, no asociado a user). | RF-TRX-02 + ADR-014 |
| AC-13 | Given `GET /api/transcriptions/{id}` con id existente del propio user, When request, Then 200 con el mismo shape que POST devolvió. | RF-MCP-06 (parcial, sin MCP wrapper) |
| AC-14 | Given `compute_type = int8_float16` configurado, When pipeline corre, Then VRAM peak ≤ 7.5 GB (deja 0.5 GB de headroom para cuda overhead). | ADR-001 + ADR-005 |
| AC-15 | Given pyannote sin HF_TOKEN, When startup, Then service fails con error claro `HF_TOKEN_MISSING_OR_INVALID`, no arranca uvicorn. | RF-TRX-06 |

---

## 4. Errores tipados (Error catalog)

| Code | HTTP | Cuándo | Body |
|---|---|---|---|
| `AUTH_NOT_AUTHENTICATED` | 401 | bearer ausente, malformado, no encontrado, revoked | `{ error_code, reason }` |
| `AUDIO_FORMAT_INVALID` | 400 | extensión no soportada o magic bytes no matchean | `{ error_code, reason: "extensión .X no soportada; soportadas: mp4, mp3, m4a, wav, flac" }` |
| `AUDIO_TOO_LARGE` | 413 | Content-Length > MAX_UPLOAD_MB | `{ error_code, reason, max_mb }` |
| `MODELS_NOT_LOADED` | 503 | startup en curso, lifespan no terminó | `{ error_code, reason: "service starting, retry in N seconds" }` + Retry-After |
| `GPU_BUSY` | 503 | lock no adquirido en LOCK_WAIT_SECONDS | `{ error_code, reason }` + Retry-After: LOCK_RETRY_AFTER_SECONDS |
| `GPU_ERROR` | 500 | CUDA OOM, illegal memory, runtime error | `{ error_code, reason, detail: <stripped> }` |
| `PIPELINE_NORMALIZE_ERROR` | 500 | ffmpeg exit ≠ 0 | `{ error_code, reason }` |
| `PIPELINE_DIARIZE_ERROR` | 500 | pyannote fail | `{ error_code, reason }` |
| `PIPELINE_TIMEOUT` | 504 | pipeline > PIPELINE_TIMEOUT_SECONDS | `{ error_code, reason, timeout_seconds }` |
| `INTERNAL_ERROR` | 500 | catch-all, log con error_id | `{ error_code, reason: "see error_id in logs", error_id }` |

---

## 5. Alternative flows

### ALT-1: Cache hit
Skip pipeline entirely. Insert new row in `transcriptions` for the user (cada user mantiene su histórico aunque el contenido se reuse). Return `cache_hit: true` en metadata.

### ALT-2: Audio extremo corto (<1 s) o silencio puro
Whisper devuelve `segments: []`. Persistir igual con `text_content: ""`, `num_speakers: 0`. Marca en metadata `silent_audio: true`. NO es error.

### ALT-3: pyannote detecta más speakers que el `max_speakers` hint
Honor el hint: re-run con `min=max=hint` o usar el resultado capped. Decisión: respetar el hint estricto. Documentar en logs `diarize_speakers_capped`.

### ALT-4: HF_TOKEN expirado durante runtime
Lifespan ya cargó pyannote en startup. Si HF_TOKEN se revoca después, pyannote sigue funcionando (modelo ya en memoria). La validación es solo en startup.

---

## 6. Data model deltas vs Capa 1

**Ninguno**. La tabla `transcriptions` ya existe con todos los campos:
- `id`, `user_id` (FK), `audio_hash`, `language`, `num_speakers`
- `text_content`, `segments` (JSONB), `extra_metadata` (JSONB)
- `original_filename`, `original_size_bytes`, `duration_seconds`
- `created_at`, `processed_at`
- GIN tsvector index sobre `text_content` (Capa 1)

`upload_sessions` queda sin uso en Capa 3 (la usaremos en Capa 4 cuando metamos `request_upload_url`).

---

## 7. Configuración (env vars nuevas)

| Variable | Default | Descripción |
|---|---|---|
| `WHISPER_MODEL` | `large-v3` | Faster-whisper model size |
| `WHISPER_BATCH_SIZE` | `8` | Inference batch (ajustar a 4 si OOM) |
| `WHISPER_DEVICE` | `cuda` | `cuda` o `cpu` para fallback dev |
| `PYANNOTE_MODEL` | `pyannote/speaker-diarization-3.1` | HF model id |
| `MAX_AUDIO_DURATION_SECONDS` | `7200` | 2h hard cap (más que eso → 413 con AUDIO_TOO_LARGE) |

Existentes que se usan tal cual: `MAX_UPLOAD_MB`, `PIPELINE_TIMEOUT_SECONDS`, `LOCK_WAIT_SECONDS`, `LOCK_RETRY_AFTER_SECONDS`, `CACHE_TTL_SECONDS`, `CACHE_CLEANUP_INTERVAL_SECONDS`, `DATA_DIR`, `HF_TOKEN`, `COMPUTE_TYPE`.

---

## 8. Estructura de módulos

```
src/transcription_api/
├── pipeline/
│   ├── __init__.py        # exports orchestrate(), load_models()
│   ├── normalize.py       # ffmpeg + sha256
│   ├── stt.py             # WhisperX wrapper, model load on startup
│   ├── diarize.py         # pyannote wrapper, model load on startup
│   ├── merge.py           # word-to-speaker assignment
│   ├── orchestrator.py    # lock + happy path + error mapping
│   ├── cache.py           # filesystem read/write per audio_hash
│   ├── cleanup.py         # asyncio task: TTL purge
│   └── errors.py          # tipados PipelineError, GPUBusy, etc.
├── api/
│   ├── __init__.py
│   └── transcriptions.py  # POST + GET endpoints
└── main.py                # lifespan: load_models() + cleanup_task()
```

---

## 9. Out of scope (Capa 4+)

- MCP server con tools `start_transcription`, `list_my_transcriptions`, `search_my_transcriptions`, `get_transcription`, `delete_transcription`.
- Two-step upload (`request_upload_url` → upload → `start_transcription`).
- Image upload + association (RF-IMG-*).
- Soft delete (`deleted_at`).
- Full-text search endpoint (RF-MCP-05).

---

## 10. Riesgos y mitigaciones

| Riesgo | Probabilidad | Impacto | Mitigación |
|---|---|---|---|
| OOM al cargar Whisper int8 + pyannote juntos | Media | Bloquea Capa 3 | AC-14 lo testa; si falla, ADR-001 contempla `large-v3-turbo` (Opción B) o Canary (Opción D) |
| WER del español rioplatense > 10% | Media | Bloquea ADR-001 | Test empírico en Batch 3 con audio de reunión real; si falla, evaluar alternativos |
| pyannote bloqueado por HF_TOKEN tier | Baja | Bloquea startup | AC-15 fail-fast en startup; HF_TOKEN ya está validado por Franco |
| ffmpeg falla en formatos raros (mp4 con codec exótico) | Baja | Falla individual de request | Mensaje de error claro `AUDIO_FORMAT_INVALID` con codec detectado |
| Lock contention en mucha carga concurrente | Baja (Sandinas: ~5 transcripciones/día) | UX degradada | 503 + Retry-After 10 min (LOCK_RETRY_AFTER_SECONDS) |
| Imagen Docker pesa demasiado tras `[pipeline]` | Alta (~10 GB con torch + cuda) | Builds lentos | Multi-stage Dockerfile; `[pipeline]` extras solo en runtime stage |

---

## 11. Trazabilidad

| Wiki | Cubierto en spec |
|---|---|
| RF-TRX-01 (cache miss) | AC-1, sección 2 |
| RF-TRX-02 (cache hit) | AC-2, AC-12, ALT-1 |
| RF-TRX-03 (validación formato/tamaño) | AC-4, AC-5 |
| RF-TRX-04 (lock global) | AC-6 |
| RF-TRX-05 (errores GPU) | AC-7 |
| RF-TRX-06 (filesystem tolerante + Postgres crítico) | AC-1, AC-7 (Postgres NO se persiste si pipeline falla) |
| RF-CACHE-01 (cleanup en startup) | sección 8 (cleanup.py) |
| RF-CACHE-02 (purge TTL vencidas) | AC-10 |
| RF-CACHE-03 (skip + log corruptas) | sección 8 (cache.py defensive) |
| RF-CACHE-04 (upload_sessions cleanup) | NO en Capa 3 (deferred) — D-026 logueada |
| RF-MCP-03 (REST upload) | parcial: solo POST /api/transcriptions, sin two-step pattern |
| ADR-014/015 (per-user scoping) | AC-8, AC-12 |

---

## 12. Definition of Done

- [ ] 15/15 ACs verdes en pytest
- [ ] Imagen Docker con `[pipeline]` extras pesa <12 GB final
- [ ] `/health` reporta `gpu_available: true` y `models = ready` en el rig
- [ ] Smoke E2E en rig: subir un mp3 real de 3 min via curl + bearer dev, ver JSON coherente
- [ ] Smoke E2E en rig: subir el MISMO archivo, confirmar cache hit (latencia <500 ms)
- [ ] VRAM peak medido durante un pipeline real, registrado en `docs/sesiones/*-capa3-vram-budget.md` para cerrar AC-14
- [ ] Drift D-026 (REST sin MCP) anotado en wiki-drifts
- [ ] Multi-agent review (igual que Capa 2) sobre el código de Capa 3 antes de mergear
