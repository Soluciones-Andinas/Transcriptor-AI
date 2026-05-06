# Módulo CACHE — Requerimientos Funcionales (Limpieza de Caché y Sesiones)

**Source flow**: [`FL-TRX-02`](../FL/FL-TRX-02.md)
**Architecture**: [`02_arquitectura.md`](../02_arquitectura.md) §3 (componentes C, K), §8 (Resiliencia)
**Data model**: [`05_modelo_datos.md`](../05_modelo_datos.md) §1, §2 (`upload_sessions`), §3
**Hardening level**: Execution-Normative

> **Nota de versión 2.0**: el cleanup ahora cubre tanto el caché filesystem efímero (sin cambios) como las `upload_sessions` vencidas en Postgres y los binarios huérfanos asociados (RF-CACHE-04 nuevo).

## Tabla resumen

| ID | Título | Actor | Pre-condición | Entradas | Salidas | Criterio de aceptación |
|---|---|---|---|---|---|---|
| RF-CACHE-01 | Configurar cleanup job en startup | FastAPI App | Servicio iniciando | env vars `CACHE_CLEANUP_INTERVAL_SECONDS`, `CACHE_TTL_SECONDS`, `UPLOAD_SESSION_GRACE_SECONDS` | Background task corriendo | Given startup, when lifespan, then task activa |
| RF-CACHE-02 | Eliminar entradas filesystem vencidas | Cleanup Job | Existe `<cache>/` con N entradas | Lista de directorios `<hash>/` | Entradas vencidas eliminadas + log | Given entrada > TTL, when barrido, then directorio eliminado |
| RF-CACHE-03 | Manejar entradas filesystem corruptas | Cleanup Job | Entrada con meta inválida | Directorio con `meta.json` corrupto | Skip + log WARN | Given meta corrupta, when barrido, then skip sin crash |
| RF-CACHE-04 | Limpiar `upload_sessions` vencidas y binarios huérfanos | Cleanup Job | Postgres reachable | Sesiones con `status IN ('requested','uploaded')` y `expires_at + grace < now()` | Sesiones marcadas `expired`; binarios huérfanos borrados | Given session vencida sin consumir, when barrido, then row marcada y blob borrado |

---

## RF-CACHE-01: Configurar cleanup job en startup

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-CACHE-01 |
| Título | Inicializar el cleanup job en el lifespan de FastAPI |
| Actor primario | FastAPI App |
| Prioridad | Alta |
| Severidad | Mayor |
| Flujo origen | FL-TRX-02 §6 |

### Precondiciones detalladas

| # | Condición | Verificación |
|---|---|---|
| 1 | Servicio iniciando (lifespan event) | FastAPI lifespan handler |
| 2 | Variable `CACHE_CLEANUP_INTERVAL_SECONDS` configurada | env var (default `3600`) |
| 3 | Variable `CACHE_TTL_SECONDS` configurada | env var (default `86400`) |
| 4 | Variable `DATA_DIR` configurada | env var |

### Inputs

| Campo | Tipo | Origen | Validación |
|---|---|---|---|
| `CACHE_CLEANUP_INTERVAL_SECONDS` | int | env var | `> 0`, default 3600 |
| `CACHE_TTL_SECONDS` | int | env var | `> 0`, default 86400 |
| `DATA_DIR` | string (path) | env var | directorio escribible |

### Process Steps (Happy Path)

| # | Paso | Componente responsable |
|---|---|---|
| 1 | Leer env vars con `pydantic_settings` | Lifespan |
| 2 | Crear directorio `<DATA_DIR>/cache/` si no existe (`os.makedirs(exist_ok=True)`) | Lifespan |
| 3 | Si recién se crea: emitir log `cache_dir_initialized` con la ruta | Lifespan |
| 4 | Crear `asyncio.Task` que ejecute en loop infinito: `await asyncio.sleep(interval)` y luego ejecutar RF-CACHE-02 | Lifespan |
| 5 | Almacenar referencia a la task en `app.state.cleanup_task` | Lifespan |
| 6 | En shutdown: cancelar la task con `task.cancel()` y `await task` con `try/except CancelledError` | Lifespan |

### Outputs

| Campo | Tipo | Destino | Efecto observable |
|---|---|---|---|
| Background task corriendo | `asyncio.Task` | proceso | El barrido se ejecuta cada `interval` segundos |
| Log `cache_cleanup_started` | log | stdout | Operador ve config inicial |

### Typed Errors

| Código | HTTP | Causa | Trigger |
|---|---|---|---|
| `STARTUP_CACHE_DIR_UNWRITABLE` | n/a | No se puede crear `cache/` | Lifespan re-lanza la excepción → contenedor crashea → Docker reinicia |

### Special Cases and Variants

- **Re-inicio del servicio mientras hay request en curso**: el shutdown cancela la cleanup task; los requests activos terminan o se cortan según healthcheck.
- **Variables mal configuradas** (`CACHE_TTL_SECONDS=0`): pydantic-settings rechaza con validación → contenedor no levanta.

### Data Model Impact

- Crea `<DATA_DIR>/cache/` si no existe.
- No modifica entradas existentes durante el startup.

### Expanded Acceptance Criteria (Gherkin)

```gherkin
Scenario: Startup crea cleanup task con valores default
  Given las env vars no están seteadas
  When el servicio levanta
  Then app.state.cleanup_task existe
    And la task está corriendo (no done)
    And el log contiene cache_cleanup_started con interval_seconds=3600

Scenario: Startup con valores custom
  Given CACHE_CLEANUP_INTERVAL_SECONDS=60 y CACHE_TTL_SECONDS=600
  When el servicio levanta
  Then el log contiene cache_cleanup_started con interval_seconds=60

Scenario: Shutdown cancela la task
  Given el servicio está corriendo con cleanup task activa
  When se dispara shutdown (SIGTERM)
  Then la task está cancelada (done con CancelledError)

Scenario: TTL inválido rompe el startup
  Given CACHE_TTL_SECONDS=0
  When el servicio intenta levantar
  Then el lifespan falla con ValidationError
    And el contenedor reinicia (Docker healthcheck)
```

### Test Traceability

| Test ID | Tipo | Cubre |
|---|---|---|
| TP-CACHE-01-pos-01 | Positivo | Startup default crea task |
| TP-CACHE-01-pos-02 | Positivo | Custom env vars se aplican |
| TP-CACHE-01-pos-03 | Positivo | Shutdown cancela task limpia |
| TP-CACHE-01-neg-01 | Negativo | TTL=0 → ValidationError en startup |

### No Ambiguities Left

- **Forbidden assumptions**: no se asume cron externo; el cleanup vive dentro del proceso.
- **Closed decisions**: TTL 24h y interval 1h son defaults configurables. ADR-004.
- **Out of scope**: monitoring del estado de la task con métricas; cron externo.

**TODO explicit = 0**.

---

## RF-CACHE-02: Eliminar entradas vencidas

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-CACHE-02 |
| Título | Iterar el caché y eliminar entradas con TTL expirado |
| Actor primario | Cleanup Job |
| Prioridad | Alta |
| Severidad | Mayor |
| Flujo origen | FL-TRX-02 §6 |

### Precondiciones detalladas

| # | Condición | Verificación |
|---|---|---|
| 1 | `<DATA_DIR>/cache/` existe | `os.path.isdir` |
| 2 | RF-CACHE-01 inicializó la task | `app.state.cleanup_task` no None |

### Inputs

Sin inputs externos. Lee del filesystem.

### Process Steps (Happy Path)

| # | Paso | Componente responsable |
|---|---|---|
| 1 | Capturar `start_time = time.monotonic()` y `now = time.time()` | Cleanup |
| 2 | Inicializar `entries_purged=0`, `bytes_freed=0` | Cleanup |
| 3 | Walk per-user (D-027): para cada `<user_id>` directorio que match UUID en `<DATA_DIR>/cache/`: | Cleanup |
| 3a | Para cada `<audio_hash>` directorio que match regex `^[0-9a-f]{64}$` dentro de `<user_id>/`: | Cleanup |
| 3b | Leer `mtime = os.stat(<user_id>/<audio_hash>/result.json).st_mtime` | Cleanup |
| 3c | Si `now - mtime > ttl_seconds`: medir tamaño, eliminar con `shutil.rmtree(<user_id>/<audio_hash>)`, incrementar contadores, emitir log `cache_entry_purged` | Cleanup |
| 3d | Si `now - mtime <= ttl_seconds`: skip silencioso | Cleanup |
| 3e | Si `result.json` no existe (entrada corrupta o legacy con meta.json + transcription.json) → delegar a RF-CACHE-03 | Cleanup |
| 4 | Cascade rmdir best-effort: `<user_id>/` queda vacío tras purgar todos sus hashes → `os.rmdir(<user_id>/)`. No-op si tiene hashes vivos. | Cleanup |
| 5 | Emitir log `cache_cleanup_completed` con `entries_purged`, `bytes_freed`, `duration_ms = (time.monotonic() - start_time) * 1000` | Cleanup |

> **Cambio de contrato Capa 3 (D-027 + D-NEW-FILENAME)**: el cache ahora vive en `<DATA_DIR>/cache/<user_id>/<audio_hash>/result.json` (single file, no más `meta.json` + `transcription.json` separados). El TTL es derivado del `mtime` del `result.json`, no de un campo `meta.created_at`. Resultado: aislamiento de privacidad estricto por user (dos users con el mismo audio NO comparten resultado), y un archivo menos por entrada. La asunción "schema_version mismatch" desaparece (no hay schema dentro de `meta.json`).

### Outputs

| Campo | Tipo | Destino | Efecto observable |
|---|---|---|---|
| Directorios `<hash>/` con TTL expirado eliminados | files removed | filesystem | Espacio liberado |
| Log `cache_entry_purged` por cada eliminación | log | stdout | Trazabilidad por hash |
| Log `cache_cleanup_completed` resumen | log | stdout | Operador ve totales |

### Typed Errors

No genera HTTP errors. Errores internos:

| Caso | Manejo |
|---|---|
| `<hash>/meta.json` no existe | Skip + log WARN (delegar a RF-CACHE-03) |
| `<hash>/meta.json` corrupto | Skip + log WARN (delegar a RF-CACHE-03) |
| `shutil.rmtree` falla con `PermissionError` | Log ERROR `cache_purge_permission_error`; continuar con la siguiente |
| Otro `OSError` durante el barrido | Log ERROR; continuar con la siguiente entrada |

### Special Cases and Variants

- **Caché vacío**: log `cache_cleanup_completed` con `entries_purged=0`. No es error.
- **Drift de reloj**: si `created_at` está en el futuro, age es negativo, no se purga. Comportamiento aceptable; log INFO `cache_entry_future_dated` con el hash.
- **Race con escritura nueva**: el lock global (RF-TRX-04) garantiza que cuando la app está escribiendo no hay otro request; el cleanup corre fuera del lock pero opera sobre entradas distintas. Si por algún motivo intenta borrar una entrada en uso, `rmtree` falla → log y skip.
- **Ciclo de cleanup más largo que el intervalo**: el siguiente ciclo espera (no se solapan porque hay una sola task).

### Data Model Impact

- Elimina `TranscriptionResult` y `CacheMeta` de las entradas vencidas.
- No modifica entradas vigentes.

### Expanded Acceptance Criteria (Gherkin)

```gherkin
Scenario: Caché con entradas mixtas
  Given el caché contiene 3 entradas:
    | hash | created_at (UTC)          | ttl_seconds |
    | aaa  | 2026-04-29T14:00:00+00:00 | 86400       |
    | bbb  | 2026-04-30T10:00:00+00:00 | 86400       |
    | ccc  | 2026-04-28T08:00:00+00:00 | 86400       |
    And la hora actual es 2026-04-30T15:00:00+00:00
  When se ejecuta el barrido
  Then aaa permanece (vencida en 1h pero aún vigente, age=25h vs ttl=24h: aaa también vencida)
    And ccc fue eliminada
    And bbb permanece
    And el log cache_cleanup_completed tiene entries_purged=2
    And el log cache_entry_purged aparece 2 veces

# Aclaración: con la hora actual 2026-04-30T15:00:00:
#   aaa: age = 25h, ttl=24h, age > ttl → ELIMINAR
#   ccc: age = 55h, ttl=24h, age > ttl → ELIMINAR
#   bbb: age = 5h, ttl=24h, age < ttl → MANTENER

Scenario: Caché vacío no rompe el barrido
  Given el directorio cache/ existe sin entradas
  When se ejecuta el barrido
  Then el log cache_cleanup_completed tiene entries_purged=0
    And no hay errores

Scenario: Entrada con created_at en el futuro
  Given el caché contiene una entrada con created_at = "2099-01-01T00:00:00+00:00"
  When se ejecuta el barrido
  Then la entrada permanece
    And el log contiene cache_entry_future_dated

Scenario: PermissionError al eliminar no aborta el barrido
  Given una entrada vencida cuyo rmtree lanza PermissionError
    And otra entrada vencida con permisos correctos
  When se ejecuta el barrido
  Then la entrada con permisos correctos fue eliminada
    And la otra permanece
    And el log contiene cache_purge_permission_error con su hash
```

### Test Traceability

| Test ID | Tipo | Cubre |
|---|---|---|
| TP-CACHE-02-pos-01 | Positivo | Mix de entradas: solo se eliminan vencidas |
| TP-CACHE-02-pos-02 | Positivo | Caché vacío no rompe |
| TP-CACHE-02-pos-03 | Positivo | Future-dated permanece |
| TP-CACHE-02-pos-04 | Positivo | Log de resumen con contadores correctos |
| TP-CACHE-02-neg-01 | Negativo (mock) | PermissionError en una entrada → otras se eliminan; log error |

### No Ambiguities Left

- **Forbidden assumptions**: no se asume orden de iteración del filesystem.
- **Closed decisions**: regex de hash es `^[0-9a-f]{64}$`; otros directorios se ignoran (no se borran). Esto previene que un directorio rogue del operador se borre por error.
- **Out of scope**: archivado a otro storage antes de eliminar; opción de dry-run.

**TODO explicit = 0**.

---

## RF-CACHE-03: Manejar entradas corruptas

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-CACHE-03 |
| Título | Skipear entradas con `meta.json` ausente o corrupto |
| Actor primario | Cleanup Job |
| Prioridad | Media |
| Severidad | Mayor |
| Flujo origen | FL-TRX-02 §7 |

### Precondiciones detalladas

| # | Condición | Verificación |
|---|---|---|
| 1 | El barrido (RF-CACHE-02) está iterando entradas | Estado del Cleanup Job |

### Inputs

Path al directorio `<DATA_DIR>/cache/<user_id>/<audio_hash>/`.

### Process Steps (Happy Path)

| # | Paso | Componente responsable |
|---|---|---|
| 1 | Intentar `open(<hash>/meta.json)` | Cleanup |
| 2 | Si `FileNotFoundError`: emitir log WARN `cache_meta_unreadable` con `audio_hash`, `reason="missing"`. Skip. | Cleanup |
| 3 | Si lectura OK: `json.load`. Si `JSONDecodeError`: emitir log WARN `cache_meta_unreadable` con `reason="json_decode_error"`. Skip. | Cleanup |
| 4 | Si JSON parsea pero falta campo obligatorio (`created_at`, `ttl_seconds`): emitir log WARN con `reason="missing_required_field"`. Skip. | Cleanup |
| 5 | Si campo `created_at` no parsea como ISO 8601: log WARN `reason="invalid_created_at"`. Skip. | Cleanup |
| 6 | Si todas las validaciones pasan: continuar con RF-CACHE-02 paso 4b | Cleanup |

### Outputs

| Campo | Tipo | Destino | Efecto observable |
|---|---|---|---|
| Log `cache_meta_unreadable` | log | stdout | Operador ve entradas sospechosas |
| Skip de la entrada | — | Cleanup loop | El barrido continúa con la siguiente |

### Typed Errors

No genera HTTP errors.

### Special Cases and Variants

- **Entrada huérfana persistente**: si la misma entrada aparece corrupta en N ciclos consecutivos (configurable, default 3), el operador puede decidir purgar manualmente. Auto-purga de huérfanas no incluida en MVP.
- **`schema_version` desconocido**: si `meta.schema_version > 1`, log WARN `cache_meta_schema_version_unknown`. Skip (la app actual no sabe interpretarlo).

### Data Model Impact

Ninguno (skip-only).

### Expanded Acceptance Criteria (Gherkin)

```gherkin
Scenario: meta.json ausente
  Given una entrada <hash>/ con transcription.json pero sin meta.json
  When se ejecuta el barrido
  Then el log contiene cache_meta_unreadable con reason="missing"
    And la entrada permanece (no se elimina)

Scenario: meta.json con JSON inválido
  Given una entrada con meta.json de contenido "{ created_at: not closed"
  When se ejecuta el barrido
  Then el log contiene cache_meta_unreadable con reason="json_decode_error"

Scenario: meta.json sin campo created_at
  Given una entrada con meta.json = {"ttl_seconds": 86400}
  When se ejecuta el barrido
  Then el log contiene cache_meta_unreadable con reason="missing_required_field"

Scenario: created_at malformado
  Given meta.json con created_at = "ayer a las 4"
  When se ejecuta el barrido
  Then el log contiene cache_meta_unreadable con reason="invalid_created_at"

Scenario: Schema version futura
  Given meta.json con schema_version=999
  When se ejecuta el barrido
  Then el log contiene cache_meta_schema_version_unknown
    And la entrada permanece
```

### Test Traceability

| Test ID | Tipo | Cubre |
|---|---|---|
| TP-CACHE-03-neg-01 | Negativo | meta.json ausente → skip |
| TP-CACHE-03-neg-02 | Negativo | JSON inválido → skip |
| TP-CACHE-03-neg-03 | Negativo | Falta campo obligatorio → skip |
| TP-CACHE-03-neg-04 | Negativo | created_at malformado → skip |
| TP-CACHE-03-neg-05 | Negativo | schema_version futura → skip |

### No Ambiguities Left

- **Forbidden assumptions**: no se intenta reparar la entrada corrupta; solo skip.
- **Closed decisions**: la entrada corrupta no se elimina; el operador decide. Justificación: borrarla automáticamente puede destruir trabajo que el operador quiere recuperar manualmente.
- **Out of scope**: auto-purga de huérfanas tras N ciclos; alertas al operador.

**TODO explicit = 0**.

---

## RF-CACHE-04: Limpiar upload_sessions vencidas y binarios huérfanos

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-CACHE-04 |
| Título | Marcar sesiones de upload vencidas y eliminar binarios huérfanos asociados |
| Actor primario | Cleanup Job |
| Prioridad | Media |
| Severidad | Mayor (sin esto, disco crece sin límite con uploads abandonados) |
| Flujo origen | FL-TRX-02 §6 (extendido en versión 2.0) |

### Precondiciones detalladas

| # | Condición | Verificación |
|---|---|---|
| 1 | Postgres reachable | health |
| 2 | Tabla `upload_sessions` existe | migración aplicada |
| 3 | Variable `UPLOAD_SESSION_GRACE_SECONDS` configurada (default 300, 5 min después del expiración) | env |

### Inputs

Sin inputs externos. Lee de Postgres y filesystem.

### Process Steps

| # | Paso |
|---|---|
| 1 | `now = datetime.now(timezone.utc)` |
| 2 | SELECT id, user_id, kind, transcription_id, status FROM upload_sessions WHERE status IN ('requested','uploaded') AND expires_at + interval '<grace> seconds' < now |
| 3 | Para cada session vencida: |
| 3a | Si `status='requested'`: solo UPDATE (no hubo upload, no hay binario que borrar) |
| 3b | Si `status='uploaded'` y `kind='audio'`: DELETE filesystem `<DATA_DIR>/uploads/<upload_id>/` |
| 3c | Si `status='uploaded'` y `kind='image'`: la imagen quedó huérfana (uploaded pero nunca attached). DELETE images row asociada (si existe) y file en `<DATA_DIR>/blobs/<user_id>/<transcription_id>/<image_id>.<ext>` |
| 3d | UPDATE upload_sessions SET status='expired' WHERE id=session_id |
| 3e | Emitir log `upload_session_expired(upload_id, user_id, kind)` |
| 4 | Emitir log `upload_session_cleanup_completed(sessions_expired, binaries_deleted, duration_ms)` |

### Outputs

| Campo | Destino |
|---|---|
| Sesiones marcadas `expired` | Postgres |
| Binarios huérfanos borrados | Filesystem |
| Logs por cada eliminación | stdout |

### Typed Errors

No genera HTTP errors. Errores internos:

| Caso | Manejo |
|---|---|
| Filesystem `rmtree` falla con `PermissionError` o `FileNotFoundError` | Log WARN, continúa con la siguiente session; no se marca como expired (se reintentará en el siguiente ciclo) |
| Postgres falla mid-cleanup | Excepción se propaga, ciclo aborta; siguiente ciclo lo retoma (idempotente) |

### Special Cases and Variants

- **Race con un user que llama `start_transcription` justo cuando el cleanup marca expired**: la query del start_transcription verifica `status='uploaded'`; si el cleanup ya cambió a `expired`, el start_transcription falla con `UPLOAD_SESSION_NOT_FOUND`. Comportamiento aceptable.
- **Grace period configurable**: default 5 min después de `expires_at`. Cubre clock drift entre cliente y server.
- **Imágenes sin row en `images`** (uploaded a filesystem pero falló el INSERT en upload-image): se identifican por upload_session sin `image_id` correspondiente. Se borran igual.

### Data Model Impact

- UPDATE en `upload_sessions` (status → 'expired').
- DELETE en `images` (cuando aplique).
- DELETE en filesystem (`<DATA_DIR>/uploads/<upload_id>/`, `<DATA_DIR>/blobs/.../<image_id>.<ext>`).

### Expanded Acceptance Criteria (Gherkin)

```gherkin
Scenario: Sesión 'requested' vencida sin upload
  Given upload_session id=U1 status='requested', expires_at=hace 10 min, grace=5 min
  When ejecuta barrido RF-CACHE-04
  Then upload_sessions.status='expired' para U1
    And no se intenta borrar archivo (no hubo)

Scenario: Sesión 'uploaded' audio vencida sin start_transcription
  Given upload_session id=U2 kind='audio', status='uploaded', expires_at hace 10 min
    And /data/uploads/U2/original.bin existe
  When ejecuta barrido
  Then status='expired'
    And /data/uploads/U2/ no existe

Scenario: Sesión 'uploaded' image vencida sin attach_image
  Given upload_session id=U3 kind='image', status='uploaded', expires_at hace 20 min
    And images row con image_id existe
    And /data/blobs/<user>/<transcription>/<image_id>.png existe
  When ejecuta barrido
  Then status='expired'
    And images row deleted (hard) o soft-deleted
    And blob borrado

Scenario: PermissionError en delete blob
  Given mock que hace rmtree lanzar PermissionError para U2
  When barrido
  Then U2 permanece status='uploaded' (se reintentará)
    And log WARN cache_purge_permission_error
    And otras sessions vencidas siguen procesándose

Scenario: Sesión vigente no se toca
  Given expires_at en el futuro
  When barrido
  Then status sigue 'requested' o 'uploaded'
```

### Test Traceability

| Test ID | Tipo | Cubre |
|---|---|---|
| TP-CACHE-04-pos-01 | Positivo | session 'requested' vencida → expired |
| TP-CACHE-04-pos-02 | Positivo | session 'uploaded' audio → expired + filesystem cleaned |
| TP-CACHE-04-pos-03 | Positivo | session 'uploaded' image → expired + blob borrado |
| TP-CACHE-04-pos-04 | Positivo | session vigente no se toca |
| TP-CACHE-04-neg-01 | Negativo | PermissionError no aborta el ciclo |

### No Ambiguities Left

- **Forbidden assumptions**: no se asume que filesystem y Postgres estén perfectamente sincronizados; el cleanup tolera divergencia (filesystem sin row, row sin filesystem).
- **Closed decisions**: grace period 5 min default; status final `expired` (no se borra la row para auditoría).
- **Out of scope**: notificación al user de que su upload abandonado fue limpiado; reintento automático del upload.

**TODO explicit = 0**.
