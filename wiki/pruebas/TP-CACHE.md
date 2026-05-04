# Test Plan — Módulo CACHE (Limpieza de Caché)

**Source RFs**: [`RF/RF-CACHE.md`](../RF/RF-CACHE.md)
**Stack de testing**: pytest 8.x + pytest-asyncio + freezegun (para manipular tiempo)

## Convenciones

- Tests sobre el cleanup usan `tmp_path` para directorios efímeros y `freezegun` para simular el paso del tiempo.
- Mock de `app.state.cleanup_task` permite ejecutar el barrido de forma síncrona en el test.

## TP-CACHE-01: Configurar cleanup job en startup

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-CACHE-01-pos-01 | Integration | Startup default crea cleanup task con interval=3600, ttl=86400 | Sin env vars | Levantar app con `TestClient` | `app.state.cleanup_task` no None; task no done; log `cache_cleanup_started` con `interval_seconds=3600`, `ttl_seconds=86400` |
| TP-CACHE-01-pos-02 | Integration | Custom env vars se aplican | `CACHE_CLEANUP_INTERVAL_SECONDS=60`, `CACHE_TTL_SECONDS=600` | Levantar app | log con `interval_seconds=60`, `ttl_seconds=600` |
| TP-CACHE-01-pos-03 | Integration | Shutdown cancela la task | App corriendo | Disparar shutdown | `app.state.cleanup_task.cancelled() == True` |
| TP-CACHE-01-pos-04 | Integration | Crear `<DATA_DIR>/cache/` si no existe | Borrar `cache/` antes de startup | Levantar app | Directorio creado; log `cache_dir_initialized` |
| TP-CACHE-01-neg-01 | Integration | TTL=0 rompe el startup | `CACHE_TTL_SECONDS=0` | Levantar app | `pydantic.ValidationError` en lifespan; el contenedor reinicia |
| TP-CACHE-01-neg-02 | Integration | `DATA_DIR` no escribible rompe startup | `DATA_DIR=/root/no-permission` | Levantar app | OSError propagado; contenedor crashea |

## TP-CACHE-02: Eliminar entradas vencidas

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-CACHE-02-pos-01 | Unit (freezegun) | Mix de entradas: solo se eliminan vencidas | 3 entradas: `aaa` (`created_at=now-25h`), `bbb` (`created_at=now-5h`), `ccc` (`created_at=now-55h`); `ttl=86400`. Hora actual fijada con freezegun. | Ejecutar barrido | `aaa` y `ccc` eliminadas; `bbb` permanece; log `cache_cleanup_completed` con `entries_purged=2`; 2 logs `cache_entry_purged` |
| TP-CACHE-02-pos-02 | Unit | Caché vacío no rompe el barrido | Directorio `cache/` vacío | Ejecutar barrido | log `cache_cleanup_completed` con `entries_purged=0`; sin errores |
| TP-CACHE-02-pos-03 | Unit (freezegun) | Future-dated permanece y loguea | Entrada con `created_at=2099-01-01T00:00:00+00:00` | Ejecutar barrido | Entrada permanece; log `cache_entry_future_dated` con su hash |
| TP-CACHE-02-pos-04 | Unit | Log de resumen tiene contadores correctos | 5 entradas, 3 vencidas | Ejecutar barrido | `entries_purged=3`; `bytes_freed > 0` (suma de tamaños eliminados) |
| TP-CACHE-02-pos-05 | Unit | Directorios que NO matchean regex hash se ignoran | Crear `cache/notes-temp/` (no es un hash) | Ejecutar barrido | `notes-temp/` permanece (no se borra accidentalmente) |
| TP-CACHE-02-neg-01 | Mock | PermissionError en una entrada no aborta el barrido | Mock que hace `shutil.rmtree` lanzar `PermissionError` solo para `aaa`; `bbb` también vencida pero borrable | Ejecutar barrido | `bbb` eliminada; `aaa` permanece; log `cache_purge_permission_error` con `aaa` |

## TP-CACHE-04: Limpiar upload_sessions vencidas y binarios huérfanos (RF-CACHE-04)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-CACHE-04-pos-01 | Unit (freezegun) | Sesión 'requested' vencida sin upload | upload_session id=U1 status='requested', expires_at hace 10 min, grace=5 min; freezegun simula tiempo | Ejecutar barrido | `upload_sessions.status='expired'` para U1; sin intentos de filesystem cleanup |
| TP-CACHE-04-pos-02 | Integration (freezegun) | Audio uploaded vencido sin start_transcription | upload_session U2 kind='audio' status='uploaded' expires_at hace 10 min; archivo `/data/uploads/U2/original.bin` existe | barrido | status='expired'; `/data/uploads/U2/` no existe; log `upload_session_expired(kind='audio')` |
| TP-CACHE-04-pos-03 | Integration (freezegun) | Image uploaded vencido sin attach | upload_session U3 kind='image' status='uploaded'; row `images` existe; blob existe en `/data/blobs/...` | barrido | status='expired'; row `images` deleted (soft o hard); blob borrado |
| TP-CACHE-04-pos-04 | Unit | Sesión vigente no se toca | expires_at en el futuro | barrido | status sigue 'requested' o 'uploaded' |
| TP-CACHE-04-neg-01 | Mock | PermissionError en delete blob | Mock que hace `rmtree` lanzar `PermissionError` para U2; otra session U4 también vencida pero sin permisos | barrido | U4 procesada OK; U2 permanece status='uploaded' (reintenta próximo ciclo); log WARN `cache_purge_permission_error` |

## TP-CACHE-03: Manejar entradas corruptas

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-CACHE-03-neg-01 | Unit | meta.json ausente → skip | Crear `<hash>/transcription.json` sin `meta.json` | Ejecutar barrido | log WARN `cache_meta_unreadable` con `reason="missing"`; entrada permanece |
| TP-CACHE-03-neg-02 | Unit | JSON inválido → skip | `<hash>/meta.json` con contenido `{ created_at: not closed` | Ejecutar barrido | log WARN con `reason="json_decode_error"`; entrada permanece |
| TP-CACHE-03-neg-03 | Unit | Falta campo obligatorio → skip | `meta.json = {"ttl_seconds": 86400}` (sin `created_at`) | Ejecutar barrido | log WARN con `reason="missing_required_field"` |
| TP-CACHE-03-neg-04 | Unit | created_at malformado → skip | `meta.json` con `created_at="ayer a las 4"` | Ejecutar barrido | log WARN con `reason="invalid_created_at"` |
| TP-CACHE-03-neg-05 | Unit | schema_version futura → skip | `meta.json` con `schema_version=999` | Ejecutar barrido | log WARN `cache_meta_schema_version_unknown`; entrada permanece |

## Helpers de testing

```python
# tests/helpers/cache_factory.py

def make_cache_entry(cache_root, audio_hash, created_at, ttl_seconds=86400, schema_version=1):
    """Crea una entrada de caché válida en disco para tests."""
    entry_dir = cache_root / audio_hash
    entry_dir.mkdir(parents=True)
    (entry_dir / "transcription.json").write_text('{"language":"es","segments":[]}')
    (entry_dir / "meta.json").write_text(json.dumps({
        "audio_hash": audio_hash,
        "original_filename": "test.mp4",
        "original_size_bytes": 1024,
        "duration_seconds": 60.0,
        "created_at": created_at.isoformat(),
        "ttl_seconds": ttl_seconds,
        "schema_version": schema_version,
    }))
    return entry_dir
```

## Ejecución

```bash
# Solo módulo CACHE
pytest tests/integration/test_cache.py -v

# Solo tests con manipulación de tiempo
pytest -k freezegun -v
```

## Cobertura objetivo

- Líneas: ≥ 90% (módulo simple, debería alcanzarse).
- Cada `reason` de `cache_meta_unreadable` está cubierto por un test.
- Cada path de RF-CACHE-02 (vencida, vigente, futura, regex no matchea) tiene un test.
