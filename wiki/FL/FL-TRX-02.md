# FL-TRX-02 — Purgar caché efímero y upload sessions vencidas

## 1. Objetivo

Eliminar automáticamente las entradas del caché filesystem cuyo TTL ha expirado, las upload sessions vencidas (sin consumir) y los binarios huérfanos, sin intervención humana, manteniendo el disco bajo control.

## 2. Alcance

**In**: barrido periódico del directorio de caché filesystem; eliminación de directorios `<hash>/` con `meta.json.created_at + ttl_seconds < now()`; barrido de `upload_sessions` con `status IN ('requested','uploaded')` y `expires_at + grace < now()`; eliminación de binarios huérfanos en `<DATA_DIR>/uploads/<upload_id>/` y `<DATA_DIR>/blobs/.../<image_id>` cuya upload session quedó expired sin consumir.

**Out**: limpieza manual por el operador, archivado externo, compresión, métricas históricas de uso del caché, soft-delete de transcripciones del histórico (eso lo hace el user vía MCP).

## 3. Actores y ownership

| Actor | Ownership |
|---|---|
| Cleanup Job | Ejecutor único; tarea de fondo dentro del proceso FastAPI. |
| Caché Filesystem | Recibe las eliminaciones; no tiene lógica propia más allá del layout de directorios. |
| Operador del Rig | No interviene en la ejecución; consulta logs si aparecen anomalías. |

## 4. Precondiciones

1. El servicio FastAPI está activo (el cleanup vive en su loop).
2. Existe el directorio raíz del caché (`<DATA_DIR>/cache/`).
3. Variables de entorno definen el intervalo de barrido (default 1 h) y el TTL por entrada (default 24 h = 86400 s).

## 5. Postcondiciones

**Éxito**:
- Las entradas con `now() - meta.created_at > meta.ttl_seconds` ya no existen en disco.
- Log estructurado registra `cache_cleanup_completed` con `entries_purged`, `bytes_freed`, `duration_ms`.
- Las entradas vigentes permanecen intactas.

**Falla parcial**:
- Si la lectura de `meta.json` de una entrada falla (corrupta), la entrada se ignora y se loguea `cache_meta_unreadable` con la ruta. No se elimina ni se modifica. El operador decide.

## 6. Secuencia principal

```mermaid
sequenceDiagram
    participant Loop as FastAPI background loop
    participant CL as Cleanup Job
    participant FS as Caché Filesystem

    Loop->>CL: Disparar cada N segundos (default 3600)
    CL->>FS: list_dir(<cache>/)
    FS-->>CL: [<hash1>, <hash2>, ...]
    loop por cada <hash>
        CL->>FS: leer <hash>/meta.json
        alt meta.json válido
            CL->>CL: ¿now - created_at > ttl?
            alt vencida
                CL->>FS: rmtree(<hash>/)
                CL->>CL: contadores += 1
            else vigente
                CL->>CL: skip
            end
        else meta.json corrupto o ausente
            CL->>CL: log warning, skip
        end
    end
    CL->>CL: log cleanup_completed
```

## 7. Camino alternativo / errores

| Condición | Manejo |
|---|---|
| Directorio `<cache>/` no existe (primera ejecución) | Crearlo y log `cache_dir_initialized`. |
| Permisos insuficientes para borrar | Log `cache_cleanup_permission_error`; el Operador debe corregir. |
| `meta.json` ausente (entrada parcial por crash anterior) | Log warning; eliminar la entrada como huérfana después de N ciclos consecutivos detectándola. |
| Borrado lento por muchas entradas | El cleanup corre en background; no bloquea requests del cliente. Si un ciclo dura > intervalo, el siguiente espera (no se solapan). |

## 8. Slice de arquitectura

Componentes activados (de [`02_arquitectura.md`](../02_arquitectura.md) §3):
- C. Caché Filesystem (lectura de `meta.json`, eliminación de directorios).
- H. Limpiador de Caché.

ADRs aplicables: [ADR-004](../ADR/ADR-004.md).

## 9. Touchpoints de datos

**Entidades inspeccionadas**:
- `<cache>/<hash>/meta.json`: campo `created_at` (epoch seconds o ISO 8601) + `ttl_seconds`.

**Entidades eliminadas**:
- Directorio completo `<cache>/<hash>/` (incluye `transcription.json` y `meta.json`).

**Eventos clave** (logs):
- `cache_cleanup_started`: con `interval_seconds`, `cache_dir`.
- `cache_meta_unreadable`: con `hash` y motivo.
- `cache_entry_purged`: con `hash`, `age_hours`.
- `cache_cleanup_completed`: con `entries_purged`, `bytes_freed`, `duration_ms`.

## 10. RF candidatos para `04_RF.md`

| RF candidato | Cubre |
|---|---|
| RF-CACHE-01 | Configurar el cleanup job en el lifespan de FastAPI con intervalo desde env var. |
| RF-CACHE-02 | Iterar entradas del caché filesystem y eliminar las vencidas (TTL configurable). |
| RF-CACHE-03 | Manejo de entradas filesystem corruptas: skip + log, sin abortar el barrido. |
| RF-CACHE-04 | Iterar `upload_sessions` y marcar/borrar las vencidas; eliminar binarios huérfanos asociados. |

## 11. Cuellos de botella, riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Cleanup corre durante un request en curso → contención de I/O. | Operación es delete-only sobre directorios distintos al `<hash>` que se está leyendo/escribiendo (lock global garantiza no choque). |
| Drift de reloj del rig hace que las entradas se purguen antes de tiempo. | Usar `time.monotonic()` para intervalos y `datetime.now(timezone.utc)` para `created_at`; documentar requisito de NTP en el rig. |
| Race con escritura: el cleanup borra una entrada justo cuando otro request la está escribiendo. | El lock global (ADR-005) evita que un request escriba mientras la app procesa otro; el cleanup corre fuera del lock pero opera sobre entradas distintas. Si hay duda, `rmtree(ignore_errors=False)` y log si falla. |
| Crece la cantidad de entradas y el barrido tarda más que el intervalo. | A 5 reuniones/día y 24 h TTL hay máximo ~5 entradas en disco; el barrido es O(5). No se contempla escalar. |

## 12. RF handoff checklist

- [x] Actor único explícito (Cleanup Job).
- [x] Diagrama mermaid del barrido.
- [x] Camino de error documentado (§7).
- [x] Estados y eventos clave listados (§9).
- [x] Cuellos de botella y mitigaciones explícitos (§11).
- [x] RFs candidatos enumerados.
- [x] No hay decisiones críticas abiertas.
- [x] Listo para `crear-rf`.
