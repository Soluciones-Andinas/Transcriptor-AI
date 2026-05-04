# Test Plan — Módulo TRX (Transcripción y Diarización)

**Source RFs**: [`RF/RF-TRX.md`](../RF/RF-TRX.md)
**Stack de testing**: pytest 8.x + pytest-asyncio + httpx (FastAPI test client) + cliente MCP del SDK
**Fixtures**: WAVs / MP4s reales en `tests/fixtures/audio/` (no commiteados; descargar con `scripts/download_test_fixtures.sh`)

> **Nota de versión 2.0**: los tests originales (TP-TRX-01-pos-01..03, neg-01, etc.) siguen vigentes pero ahora se ejecutan invocando el pipeline a través de la tool MCP `start_transcription` (no por HTTP REST directo). Se agregan tests nuevos (`-pos-04`) que validan la persistencia adicional en Postgres `transcriptions` (cubre el cambio de RF-TRX-01 paso 19 y RF-TRX-02 paso 8).

## Convenciones

- Naming: `TP-<RF_ID>-<tipo>-<NN>` donde tipo ∈ `{pos, neg, cov}` (positivo, negativo, cobertura).
- Toda llamada a modelos costosos (Whisper, pyannote) se mockea en tests unitarios; los tests E2E usan modelos reales con audios cortos (< 30 s).
- Fixtures con audio real se versionan por contenido (hash del audio en el filename).

## TP-TRX-01: Procesar archivo (cache miss)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-TRX-01-pos-01 | E2E | MP4 de 60 s con 2 hablantes en español retorna 200 con TranscriptionResult válido | Fixture `meeting_60s.mp4`; caché vacío; modelos cargados | `POST /transcribe` con file=meeting_60s.mp4 | status=200; body cumple schema; segments ≥ 1; `metadata.cache_hit=false`; existen `<hash>/transcription.json` y `meta.json`; log `request_completed` con `cache_hit=false` |
| TP-TRX-01-pos-02 | E2E | Audio sin habla (silencio) retorna 200 con segments vacíos | WAV de 30 s solo silencio | `POST /transcribe` | status=200; `segments=[]`; `num_speakers=0`; entrada de caché creada |
| TP-TRX-01-pos-03 | Parametric E2E | Mismo contenido en MP4/MP3/WAV genera mismo resultado | 3 archivos con mismo audio en distinto contenedor | `POST /transcribe` con cada uno | Los 3 generan el mismo `audio_hash` y devuelven `segments` semánticamente equivalentes (text idéntico) |
| TP-TRX-01-neg-01 | Mock | Excepción no clasificada en orquestador → 500 INTERNAL_ERROR | Mock que lanza `ValueError` en el merge | `POST /transcribe` con file válido | status=500; `error_code=INTERNAL_ERROR`; lock liberado |
| TP-TRX-01-cov-01 | Cobertura | Cada paso emite el log estructurado esperado | Captura de logs con `caplog` | tool `start_transcription` | Aparecen en orden: `mcp_request_received`, `audio_normalized`, `cache_lookup`, `stt_completed`, `diarize_completed`, `merge_completed`, `cache_persisted`, `transcription_persisted`, `mcp_request_completed` |
| TP-TRX-01-pos-04 | E2E | **Nuevo (v2.0)**: persistencia en Postgres tras cache miss | Pipeline cache miss exitoso | tool `start_transcription` | Existe row en `transcriptions` con `user_id` del bearer, `audio_hash`, `segments` JSONB completo, `text` con full text |

## TP-TRX-02: Devolver resultado cacheado (cache hit)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-TRX-02-pos-01 | E2E | Mismo audio en TTL devuelve 200 < 10 s | Pre-poblar caché con `<hash>/transcription.json` y `meta.json` con `created_at=now-1h`, `ttl_seconds=86400` | `POST /transcribe` con audio matching | status=200; `metadata.served_from_cache=true`; `total_duration_ms < 10000`; log `cache_lookup` con `hit=true` |
| TP-TRX-02-pos-02 | E2E | Audio en MP3 hace cache hit de entrada creada por MP4 | Pre-poblar con resultado del MP4 | `POST /transcribe` con MP3 (mismo contenido) | `served_from_cache=true` (porque hash del WAV normalizado coincide) |
| TP-TRX-02-pos-03 | Unit | Cache hit no modifica `created_at` | Entrada con `created_at=2026-04-30T10:00:00Z` | `POST /transcribe` | `meta.json` tras la request sigue teniendo `created_at=2026-04-30T10:00:00Z` |
| TP-TRX-02-neg-01 | E2E | Entrada vencida → cache miss y reproceso | Entrada con `created_at=now-25h`, `ttl_seconds=86400` | `POST /transcribe` | Se ejecuta pipeline completo; `created_at` se actualiza a `now`; log `cache_lookup` con `hit=false` |
| TP-TRX-02-neg-02 | Mock | `transcription.json` corrupto → 500 INTERNAL_ERROR | Pre-poblar `<hash>/transcription.json` con bytes inválidos | `POST /transcribe` con audio matching | status=500; `error_code=INTERNAL_ERROR` |
| TP-TRX-02-neg-03 | Unit | `schema_version` desactualizado fuerza miss | Entrada con `meta.schema_version=99` | tool `start_transcription` con audio matching | Pipeline completo se ejecuta; entrada se sobreescribe con `schema_version=1` |
| TP-TRX-02-pos-04 | E2E | **Nuevo (v2.0)**: persistencia en Postgres tras cache hit | Cache filesystem pre-poblado | tool `start_transcription` | 200 con `cache_hit=true`; row nueva en `transcriptions` para el user actual; row distinta a la del owner original (cada user tiene su histórico) |

## TP-TRX-03: Validar formato y tamaño del upload

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-TRX-03-pos-01 | Unit | MP4 de 100 MB con extensión válida pasa validación | `MAX_UPLOAD_MB=500` | `POST /transcribe` con MP4 100 MB | Pasa al siguiente paso; no devuelve 4xx |
| TP-TRX-03-neg-01 | Unit | Archivo de 600 MB → 413 FILE_TOO_LARGE | `MAX_UPLOAD_MB=500` | `POST /transcribe` con MP4 600 MB | status=413; `error_code=FILE_TOO_LARGE` |
| TP-TRX-03-neg-02 | Unit | `.txt` → 400 UNSUPPORTED_EXTENSION | — | `POST /transcribe` con `archivo.txt` | status=400; `error_code=UNSUPPORTED_EXTENSION` |
| TP-TRX-03-neg-03 | Unit | Sin extensión → 400 UNSUPPORTED_EXTENSION | — | `POST /transcribe` con `archivo` (sin sufijo) | status=400; `error_code=UNSUPPORTED_EXTENSION` |
| TP-TRX-03-neg-04 | Unit | 0 bytes → 400 INVALID_FORMAT | — | `POST /transcribe` con MP4 vacío | status=400; `error_code=INVALID_FORMAT` |
| TP-TRX-03-neg-05 | Unit | `min_speakers=8 > max_speakers=2` → 400 INVALID_PARAMETER | — | `POST /transcribe` con file válido y params inválidos | status=400; `error_code=INVALID_PARAMETER` |

## TP-TRX-04: Manejar concurrencia con lock global

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-TRX-04-pos-01 | E2E | 2 requests serializados completan correctamente | Lock libre | Disparar A y B con 6s de delay (B espera que A termine) | Ambos status=200 |
| TP-TRX-04-pos-02 | Unit | Lock se libera tras éxito | — | Mock pipeline exitoso | `app.state.transcription_lock.locked() == False` post-request |
| TP-TRX-04-pos-03 | Unit | Lock se libera tras excepción | Mock pipeline que lanza | `POST /transcribe` | Excepción propagada; lock liberado |
| TP-TRX-04-neg-01 | E2E | 2 requests simultáneos: el segundo recibe 503 | Disparar A; mientras A procesa, disparar B | A: status=200; B: status=503; `error_code=LOCK_BUSY`; header `Retry-After: 600` |
| TP-TRX-04-neg-02 | Unit | Cliente desconecta antes de adquirir lock → no side effects | Cliente cierra conexión durante espera | Verificar | Lock no se adquirió; sin entrada de caché creada |

## TP-TRX-05: Manejar errores de GPU

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-TRX-05-neg-01 | Mock | OOM en STT → 500 CUDA_OOM stage=stt | Mock WhisperX que lanza `torch.cuda.OutOfMemoryError` | `POST /transcribe` con file válido | status=500; `error_code=CUDA_OOM`; `stage=stt`; lock liberado; sin caché |
| TP-TRX-05-neg-02 | Mock | OOM en diarización → 500 CUDA_OOM stage=diarize | Mock pyannote que lanza OOM (después de STT exitoso) | `POST /transcribe` | status=500; `stage=diarize` |
| TP-TRX-05-neg-03 | Mock | RuntimeError genérico en STT → 500 MODEL_FAILURE | Mock que lanza `RuntimeError("internal")` | `POST /transcribe` | status=500; `error_code=MODEL_FAILURE`; log con `exception_class=RuntimeError` |
| TP-TRX-05-pos-01 | E2E | Tras error, próximo request funciona | Provocar TP-TRX-05-neg-01, después request normal | request normal | status=200 (lock disponible, VRAM liberada por `empty_cache`) |

## TP-TRX-06: Persistencia tolerante a fallos de disco

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-TRX-06-pos-01 | E2E | Persistencia normal genera ambos archivos | Pipeline exitoso, disco con espacio | `POST /transcribe` | Existen `transcription.json` y `meta.json`; log `cache_persisted` |
| TP-TRX-06-neg-01 | Mock | OSError 28 (disk full) → log WARN, 200 al cliente | Monkey-patch `open()` para que lance `OSError(28, "No space left")` durante escritura | `POST /transcribe` | status=200 al cliente con TranscriptionResult; log WARN `cache_persist_failed`; sin archivos parciales |
| TP-TRX-06-neg-02 | Mock | PermissionError → log WARN, 200 al cliente | Monkey-patch `open()` para `PermissionError` | `POST /transcribe` | Idem; log `cache_persist_failed` con error |
| TP-TRX-06-neg-03 | Unit | Crash entre tmp y rename no deja archivos finales | Inyectar excepción justo antes de `os.rename` | Verificar filesystem | No existen `transcription.json` ni `meta.json`; lookup posterior es cache miss |

## Estrategia de fixtures

| Fixture | Origen | Tamaño | Cómo generar |
|---|---|---|---|
| `silence_30s.wav` | Sintético | ~1 MB | `ffmpeg -f lavfi -i anullsrc=r=16000:cl=mono -t 30 -c:a pcm_s16le silence_30s.wav` |
| `meeting_60s.mp4` | Real (interno) | ~5 MB | Grabación de prueba interna con 2 hablantes, 60 s |
| `meeting_short.mp3` | Mismo audio que meeting_60s pero en MP3 | ~1 MB | `ffmpeg -i meeting_60s.mp4 -vn -c:a libmp3lame meeting_short.mp3` |
| `meeting_short.wav` | Mismo audio que meeting_60s pero en WAV mono 16 kHz | ~2 MB | `ffmpeg -i meeting_60s.mp4 -vn -ac 1 -ar 16000 meeting_short.wav` |
| `large_600mb.mp4` | Sintético | 600 MB | `ffmpeg -f lavfi -i sine=frequency=440 -t 14400 -b:v 1k -b:a 320k large_600mb.mp4` o equivalente |
| `empty.mp4` | Sintético | 0 bytes | `touch empty.mp4` |
| `corrupt.mp4` | Sintético | 1 KB | `head -c 1024 /dev/urandom > corrupt.mp4` |

## Ejecución

```bash
# Test suite completa
pytest tests/

# Solo módulo TRX
pytest tests/integration/test_trx.py -v

# Cobertura
pytest --cov=src --cov-report=html
```

## Cobertura objetivo

- Líneas: ≥ 80%.
- Branches: ≥ 75%.
- Cada `error_code` documentado en `05_modelo_datos.md` §7 está cubierto por al menos un test.
- Cada flow path de `FL-TRX-01` (hit y miss) tiene cobertura E2E.
