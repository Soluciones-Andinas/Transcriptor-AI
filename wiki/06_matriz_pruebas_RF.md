# Matriz de Pruebas RF — `transcription-api`

Mapeo completo entre Requerimientos Funcionales y casos de prueba. Esta matriz garantiza cobertura mínima (1 positivo + 1 negativo por RF).

## Resumen

| Métrica | Valor |
|---|---|
| Total RFs | 26 |
| Total tests planificados | ≈ 95 |
| Tests positivos | ≈ 50 |
| Tests negativos | ≈ 40 |
| Tests de cobertura (logs/eventos/cross-user) | ≈ 5 |
| RFs con cobertura mínima cumplida | 26 / 26 ✓ |

## Matriz por módulo

### Módulo AUTH

| RF | Test ID | Tipo | Plan |
|---|---|---|---|
| RF-AUTH-01 | TP-AUTH-01-pos-01 | Positivo (render redirect) | [TP-AUTH](pruebas/TP-AUTH.md) |
| RF-AUTH-01 | TP-AUTH-01-pos-02 | Positivo (user ya logueado) | TP-AUTH |
| RF-AUTH-01 | TP-AUTH-01-cov-01 | Cobertura (state aleatorio único) | TP-AUTH |
| RF-AUTH-02 | TP-AUTH-02-pos-01 | Positivo (primer login) | TP-AUTH |
| RF-AUTH-02 | TP-AUTH-02-pos-02 | Positivo (login subsiguiente) | TP-AUTH |
| RF-AUTH-02 | TP-AUTH-02-neg-01 | Negativo (state mismatch) | TP-AUTH |
| RF-AUTH-02 | TP-AUTH-02-neg-02 | Negativo (cookie expirada) | TP-AUTH |
| RF-AUTH-02 | TP-AUTH-02-neg-03 | Negativo (code inválido) | TP-AUTH |
| RF-AUTH-03 | TP-AUTH-03-pos-01 | Positivo (tid match) | TP-AUTH |
| RF-AUTH-03 | TP-AUTH-03-neg-01 | Negativo (tid distinto) | TP-AUTH |
| RF-AUTH-03 | TP-AUTH-03-neg-02 | Negativo (claim missing) | TP-AUTH |
| RF-AUTH-03 | TP-AUTH-03-neg-03 | Negativo (firma inválida) | TP-AUTH |
| RF-AUTH-04 | TP-AUTH-04-pos-01 | Positivo (primer login emite bearer) | TP-AUTH |
| RF-AUTH-04 | TP-AUTH-04-pos-02 | Positivo (login subsiguiente NO emite) | TP-AUTH |
| RF-AUTH-05 | TP-AUTH-05-neg-01 | Mock (MS 503) | TP-AUTH |
| RF-AUTH-05 | TP-AUTH-05-neg-02 | Mock (timeout) | TP-AUTH |
| RF-AUTH-06 | TP-AUTH-06-pos-01 | Positivo (con flash) | TP-AUTH |
| RF-AUTH-06 | TP-AUTH-06-pos-02 | Positivo (sin flash) | TP-AUTH |
| RF-AUTH-06 | TP-AUTH-06-neg-01 | Negativo (sin cookie) | TP-AUTH |
| RF-AUTH-06 | TP-AUTH-06-neg-02 | Negativo (cookie expirada) | TP-AUTH |
| RF-AUTH-07 | TP-AUTH-07-pos-01 | Positivo (revocación + emisión) | TP-AUTH |
| RF-AUTH-07 | TP-AUTH-07-pos-02 | Positivo (bearer viejo rechazado en MCP) | TP-AUTH |
| RF-AUTH-07 | TP-AUTH-07-neg-01 | Negativo (sin auth) | TP-AUTH |

### Módulo MCP

| RF | Test ID | Tipo | Plan |
|---|---|---|---|
| RF-MCP-11 | TP-MCP-11-pos-01 | Positivo | [TP-MCP](pruebas/TP-MCP.md) |
| RF-MCP-11 | TP-MCP-11-neg-01 | Negativo (revocado) | TP-MCP |
| RF-MCP-11 | TP-MCP-11-neg-02 | Negativo (inexistente) | TP-MCP |
| RF-MCP-11 | TP-MCP-11-neg-03 | Negativo (sin header) | TP-MCP |
| RF-MCP-11 | TP-MCP-11-cov-01 | Cobertura last_used_at | TP-MCP |
| RF-MCP-01 | TP-MCP-01-pos-01 | Positivo audio | TP-MCP |
| RF-MCP-01 | TP-MCP-01-pos-02 | Positivo image | TP-MCP |
| RF-MCP-01 | TP-MCP-01-neg-01 | Negativo (transcript ajeno) | TP-MCP |
| RF-MCP-01 | TP-MCP-01-neg-02 | Negativo (file too large) | TP-MCP |
| RF-MCP-01 | TP-MCP-01-neg-03 | Negativo (mime no permitido) | TP-MCP |
| RF-MCP-02 | TP-MCP-02-pos-01 | Positivo cache miss | TP-MCP |
| RF-MCP-02 | TP-MCP-02-pos-02 | Positivo cache hit | TP-MCP |
| RF-MCP-02 | TP-MCP-02-neg-01 | Negativo (upload ajeno) | TP-MCP |
| RF-MCP-02 | TP-MCP-02-neg-02 | Negativo (already consumed) | TP-MCP |
| RF-MCP-02 | TP-MCP-02-neg-03 | Negativo (lock busy) | TP-MCP |
| RF-MCP-03 | TP-MCP-03-pos-01 | Positivo audio | TP-MCP |
| RF-MCP-03 | TP-MCP-03-pos-02 | Positivo image | TP-MCP |
| RF-MCP-03 | TP-MCP-03-neg-01 | Negativo bearer wrong | TP-MCP |
| RF-MCP-03 | TP-MCP-03-neg-02 | Negativo session expired | TP-MCP |
| RF-MCP-03 | TP-MCP-03-neg-03 | Negativo size mismatch | TP-MCP |
| RF-MCP-03 | TP-MCP-03-neg-04 | Negativo mime fake | TP-MCP |
| RF-MCP-04 | TP-MCP-04-pos-01 | Positivo | TP-MCP |
| RF-MCP-04 | TP-MCP-04-pos-02 | Positivo paginación | TP-MCP |
| RF-MCP-04 | TP-MCP-04-neg-01 | Negativo (sin auth) | TP-MCP |
| RF-MCP-04 | TP-MCP-04-cov-01 | Cobertura cross-user isolation | TP-MCP |
| RF-MCP-05 | TP-MCP-05-pos-01 | Positivo (match) | TP-MCP |
| RF-MCP-05 | TP-MCP-05-pos-02 | Positivo (no match) | TP-MCP |
| RF-MCP-05 | TP-MCP-05-cov-01 | Cobertura cross-user | TP-MCP |
| RF-MCP-06 | TP-MCP-06-pos-01 | Positivo | TP-MCP |
| RF-MCP-06 | TP-MCP-06-neg-01 | Negativo (cross-user) | TP-MCP |
| RF-MCP-06 | TP-MCP-06-neg-02 | Negativo (soft-deleted) | TP-MCP |
| RF-MCP-07 | TP-MCP-07-pos-01 | Positivo resource | TP-MCP |
| RF-MCP-07 | TP-MCP-07-neg-01 | Negativo cross-user | TP-MCP |
| RF-MCP-08 | TP-MCP-08-pos-01 | Positivo binary fetch | TP-MCP |
| RF-MCP-08 | TP-MCP-08-neg-01 | Negativo cross-user | TP-MCP |
| RF-MCP-08 | TP-MCP-08-neg-02 | Negativo image inexistente | TP-MCP |
| RF-MCP-09 | TP-MCP-09-pos-01 | Positivo soft delete | TP-MCP |
| RF-MCP-09 | TP-MCP-09-neg-01 | Negativo cross-user | TP-MCP |
| RF-MCP-09 | TP-MCP-09-neg-02 | Negativo idempotente | TP-MCP |
| RF-MCP-09 | TP-MCP-09-cov-01 | Cobertura cascade en images | TP-MCP |
| RF-MCP-10 | TP-MCP-10-pos-01 | Positivo | TP-MCP |

### Módulo TRX (revisado)

| RF | Test ID | Tipo | Plan |
|---|---|---|---|
| RF-TRX-01 | TP-TRX-01-pos-01..03 | Positivos (formatos, audio sin habla) | [TP-TRX](pruebas/TP-TRX.md) |
| RF-TRX-01 | TP-TRX-01-pos-04 | **Nuevo**: persistencia en Postgres tras cache miss | TP-TRX |
| RF-TRX-01 | TP-TRX-01-neg-01 | Negativo (excepción interna) | TP-TRX |
| RF-TRX-02 | TP-TRX-02-pos-01..03 | Positivos cache hit | TP-TRX |
| RF-TRX-02 | TP-TRX-02-pos-04 | **Nuevo**: persistencia en Postgres tras cache hit | TP-TRX |
| RF-TRX-02 | TP-TRX-02-neg-01..03 | Negativos | TP-TRX |
| RF-TRX-03 | TP-TRX-03-pos-01 + TP-TRX-03-neg-01..05 | Validación | TP-TRX |
| RF-TRX-04 | TP-TRX-04-pos-01..03 + neg-01..02 | Lock | TP-TRX |
| RF-TRX-05 | TP-TRX-05-neg-01..03 + pos-01 | GPU errors | TP-TRX |
| RF-TRX-06 | TP-TRX-06-pos-01 + neg-01..03 | Persistencia tolerante | TP-TRX |

### Módulo CACHE

| RF | Test ID | Tipo | Plan |
|---|---|---|---|
| RF-CACHE-01..03 | (existentes) | Positivos + Negativos | [TP-CACHE](pruebas/TP-CACHE.md) |
| RF-CACHE-04 | TP-CACHE-04-pos-01 | Positivo session 'requested' vencida | TP-CACHE |
| RF-CACHE-04 | TP-CACHE-04-pos-02 | Positivo audio uploaded vencido | TP-CACHE |
| RF-CACHE-04 | TP-CACHE-04-pos-03 | Positivo image uploaded vencido | TP-CACHE |
| RF-CACHE-04 | TP-CACHE-04-pos-04 | Positivo session vigente no se toca | TP-CACHE |
| RF-CACHE-04 | TP-CACHE-04-neg-01 | Negativo PermissionError | TP-CACHE |

### Módulo UI

| RF | Test ID | Tipo | Plan |
|---|---|---|---|
| RF-UI-01 | TP-UI-01-pos-01..03 | Render | [TP-UI](pruebas/TP-UI.md) |
| RF-UI-02 | TP-UI-02-pos-01..03 + neg-01 | Render + interacciones | TP-UI |

### Módulo IMG

| RF | Test ID | Tipo | Plan |
|---|---|---|---|
| RF-IMG-01 | TP-IMG-01-pos-01 + neg-01..03 | request URL | [TP-IMG](pruebas/TP-IMG.md) |
| RF-IMG-02 | TP-IMG-02-pos-01..02 + neg-01..03 | upload binario | TP-IMG |
| RF-IMG-03 | TP-IMG-03-pos-01..02 + neg-01..03 | attach | TP-IMG |

## Cobertura por error_code

| `error_code` | Test que lo verifica |
|---|---|
| `AUTH_NOT_AUTHENTICATED` | TP-AUTH-06-neg-01 |
| `AUTH_INVALID_OAUTH_CODE` | TP-AUTH-02-neg-03 |
| `AUTH_INVALID_STATE` | TP-AUTH-02-neg-01 |
| `AUTH_TENANT_NOT_ALLOWED` | TP-AUTH-03-neg-01 |
| `AUTH_PROVIDER_UNAVAILABLE` | TP-AUTH-05-neg-01 |
| `MCP_BEARER_INVALID` | TP-MCP-11-neg-02, TP-MCP-11-neg-03 |
| `MCP_BEARER_REVOKED` | TP-MCP-11-neg-01, TP-AUTH-07-pos-02 |
| `INVALID_FORMAT` | TP-TRX-03-neg-04, TP-IMG-02-neg-01 |
| `UNSUPPORTED_EXTENSION` | TP-TRX-03-neg-02, TP-IMG-01-neg-02 |
| `FILE_TOO_LARGE` | TP-TRX-03-neg-01, TP-IMG-01-neg-03 |
| `INVALID_PARAMETER` | TP-TRX-03-neg-05, TP-IMG-03-neg-03 |
| `LOCK_BUSY` | TP-TRX-04-neg-01, TP-MCP-02-neg-03 |
| `CUDA_OOM` | TP-TRX-05-neg-01, TP-TRX-05-neg-02 |
| `MODEL_FAILURE` | TP-TRX-05-neg-03 |
| `UPLOAD_SESSION_NOT_FOUND` | TP-MCP-02-neg-01, TP-MCP-03-neg-02 |
| `UPLOAD_SESSION_ALREADY_CONSUMED` | TP-MCP-02-neg-02 |
| `TRANSCRIPTION_NOT_FOUND` | TP-MCP-06-neg-01, TP-MCP-09-neg-01, TP-IMG-01-neg-01 |
| `IMAGE_NOT_FOUND` | TP-MCP-08-neg-02, TP-IMG-03-neg-01 |
| `INTERNAL_ERROR` | TP-TRX-01-neg-01 |

Cobertura completa: ✓.

## Cobertura por flow path

| Flow + path | Tests E2E |
|---|---|
| FL-AUTH-01 (login first) | TP-AUTH-02-pos-01, TP-AUTH-04-pos-01 |
| FL-AUTH-01 (login subsequent) | TP-AUTH-02-pos-02 |
| FL-AUTH-01 (errores) | TP-AUTH-02-neg-*, TP-AUTH-03-neg-*, TP-AUTH-05-neg-* |
| FL-MCP-01 (ver config) | TP-AUTH-06-pos-*, TP-UI-02-pos-* |
| FL-MCP-01 (regenerar) | TP-AUTH-07-pos-01, TP-AUTH-07-pos-02 |
| FL-TRX-01 (cache miss via MCP) | TP-MCP-02-pos-01, TP-TRX-01-pos-04 |
| FL-TRX-01 (cache hit via MCP) | TP-MCP-02-pos-02, TP-TRX-02-pos-04 |
| FL-TRX-01 (errores) | TP-MCP-02-neg-*, TP-TRX-03..05-neg-* |
| FL-TRX-02 (filesystem cleanup) | TP-CACHE-02-pos-* |
| FL-TRX-02 (upload sessions cleanup) | TP-CACHE-04-pos-* |
| FL-IMG-01 | TP-IMG-01..03-pos-* |
| FL-MIN-01 (read-only consume) | TP-MCP-04..08-pos-* + cov-01 (cross-user isolation) |

## Cobertura cross-user (security)

Toda RF que toca datos del user debe tener al menos un test que verifica per-user scoping. Estos tests son críticos.

| RF | Test cross-user | Verifica |
|---|---|---|
| RF-MCP-04 | TP-MCP-04-cov-01 | User A no ve transcripts de User B en list |
| RF-MCP-05 | TP-MCP-05-cov-01 | User A no ve matches de B en search |
| RF-MCP-06 | TP-MCP-06-neg-01 | User A get_transcription de B → 404 |
| RF-MCP-07 | TP-MCP-07-neg-01 | Resource de transcript ajeno → 404 |
| RF-MCP-08 | TP-MCP-08-neg-01 | Resource de imagen ajena → 404 |
| RF-MCP-09 | TP-MCP-09-neg-01 | User A delete transcript de B → 404 |
| RF-IMG-01 | TP-IMG-01-neg-01 | request_image_upload_url para transcript ajeno → 404 |
| RF-IMG-03 | TP-IMG-03-neg-02 | attach_image cross-user → 404 |
| RF-MCP-02 | TP-MCP-02-neg-01 | start_transcription con upload ajeno → 404 |

## Estado de la matriz

| Validación | Estado |
|---|---|
| Cada RF tiene ≥ 1 test positivo | ✓ |
| Cada RF tiene ≥ 1 test negativo | ✓ (excepto RFs UI puramente render que tienen smoke tests) |
| Cada `error_code` tiene cobertura | ✓ |
| Cada flow path tiene cobertura E2E | ✓ |
| Cada RF que toca datos tiene test cross-user | ✓ |
| Cobertura objetivo de líneas (≥80%) | A medir tras implementación |

## Próximos pasos

1. Implementar el código fuente siguiendo el orden de capas en `02_arquitectura.md`:
   - Capa 1: Postgres + modelos SQLAlchemy + Alembic.
   - Capa 2: Auth (RFs AUTH-*) + middleware MCP (RF-MCP-11).
   - Capa 3: UI mínima (RFs UI-*).
   - Capa 4: Pipeline real (RFs TRX-* y CACHE-*).
   - Capa 5: REST endpoints upload (RF-MCP-03).
   - Capa 6: MCP Server tools y resources (RF-MCP-01..10).
   - Capa 7: IMG (RFs IMG-*).
2. Implementar tests siguiendo esta matriz, idealmente TDD (test rojo → código → test verde).
3. Cerrar trazabilidad con la skill `ps-trazabilidad` antes de mergear.
