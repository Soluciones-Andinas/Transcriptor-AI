# Wiki drift audit — Capas 1-4 consolidado (2026-05-11)

> **Propósito**: barrido multi-agente paralelo wiki ↔ código tras el merge de Capa 4 (MCP) y el deploy validado en el rig (2026-05-08). Continúa la numeración de `2026-05-05-wiki-drifts.md` desde **D-050** (último D-NNN en el log previo: D-049).
>
> **Metodología**: 4 subagentes en paralelo, particionados por capa (ADRs+Top, Auth, Pipeline, MCP+UI+DB), con scope disjunto y bloque D-NNN reservado a cada uno. Cada agente leyó wiki + código relevante, verificó 1:1 cada claim, y reportó drifts nuevos con severidad + propagación + resolución sugerida.
>
> **Convención de severidad** (heredada de log previo):
> - 🔴 **CRITICAL** — silent-leak / contract break / bloquea funcionalidad end-to-end
> - 🟠 **HIGH** — bug en prod si nadie consulta el wiki / contrato externo roto
> - 🟡 **MEDIUM** — fricción ops / trazabilidad rota / inconsistencia interna
> - 🟢 **LOW** — cosmético / detalle de implementación
>
> **Total**: 40 drifts únicos (44 raw reportados, 4 consolidados en 2 macro-drifts cross-domain).

---

## Resumen ejecutivo

| Capa / Dominio | Drifts | 🔴 | 🟠 | 🟡 | 🟢 | Verificaciones 1:1 |
|---|---|---|---|---|---|---|
| ADRs + Top wiki | 9 | 1 | 3 | 4 | 1 | 22 |
| Auth | 15 | 0 | 4 | 8 | 3 | 40 |
| Pipeline | 10 | 2 | 5 | 3 | 0 | 24 |
| MCP + UI + DB | 10 | 2 | 4 | 3 | 1 | 30 |
| **Total raw** | **44** | **5** | **16** | **18** | **5** | **116** |
| **Consolidado** | **40** | **4** | **15** | **17** | **4** | — |

### Drifts por dominio (rangos D-NNN)

| Bloque | Dominio | Agente |
|---|---|---|
| D-050 – D-058 | ADRs + Top wiki + decision priority | Agent 1 |
| D-059 – D-072 | Auth (RF-AUTH + FL-AUTH-01 + TP-AUTH + scoping) | Agent 2 |
| D-073 – D-080 | Pipeline (RF-TRX + RF-CACHE + FL-TRX-01/02) | Agent 3 |
| D-081 – D-089 | MCP + UI + Image + DB models | Agent 4 |

### Cross-domain macro-drifts (alto impacto)

| ID | Tema | Severidad | Capas afectadas |
|---|---|---|---|
| **D-052** | **Taxonomía de error codes** desalineada wiki ↔ código en 7+ códigos | 🔴 CRITICAL | Pipeline, MCP/IMG, Auth |
| **D-059** | **Logs estructurados** wiki-spec'd no emitidos por código (auth + pipeline) | 🟠 HIGH | Auth, Pipeline, Cleanup |

---

## Plan de fix priorizado

> Orden de ataque sugerido por blast radius (CRITICAL primero, agrupado por archivo a tocar para minimizar churn).

### Bloque 1 — CRITICAL (bloqueantes / contract-level)

1. **D-081** — Corregir `mcp_url` doble prefix: el código sirve en `/mcp/mcp` pero `/auth/me` devuelve `/mcp` → onboarding roto. Decidir entre pasar `streamable_http_path="/"` a `FastMCP` (fix código, mantiene URL pública) o ajustar `_mcp_url()` a `/mcp/mcp` (fix doc, URL feo). Recomiendo opción A.
2. **D-050** — ADR-013 describe upload bearer como OAuth del MCP; código usa bearer efímero hash separado (`upload_bearer_hash`). Crear ADR-017 que reemplaza ADR-013 o editar in-place con suspensión de inmutabilidad (precedente D-009).
3. **D-052** — Sincronizar taxonomía de error codes (afecta 3 capas). Reescribir `wiki/05_modelo_datos.md §8` con la lista as-built (`AUDIO_TOO_LARGE`, `GPU_BUSY`, `GPU_ERROR`, `PIPELINE_TIMEOUT`, `PIPELINE_DIARIZE_ERROR`, `PIPELINE_NORMALIZE_ERROR`, `AUDIO_FORMAT_INVALID`, `DB_POOL_EXHAUSTED`, `REQUEST_TIMEOUT`, `AUTH_INVALID_STATE`, `AUTH_PROVIDER_UNAVAILABLE`, `INVALID_PARAMETER` para MIME). Eliminar `LOCK_BUSY`, `CUDA_OOM`, `MODEL_FAILURE`, `UNSUPPORTED_EXTENSION` (no existen en código).
4. **D-080** — Audio format whitelist: documentar el magic-byte fallback en `normalize.py` (acepta extensión hostile si magic bytes coinciden con MP3/WAV/FLAC/MP4). Wiki dice "solo extensión + ffmpeg"; código hace más. Actualizar RF-TRX-03 §Closed Decisions.

### Bloque 2 — HIGH (contract-level, fix corto)

5. **D-051** — `config.py`: borrar segunda declaración de `upload_session_grace_seconds` (línea 155-157). Default real es 300s en vez de 30s; ventana de upload es 10× la intendida. Agregar test de unicidad de fields.
6. **D-053** — C4 diagram en `wiki/02_arquitectura.md §3` dice cache "Por audio_hash"; corregir a "Per-user (user_id, audio_hash), TTL 24h".
7. **D-073** — RF-CACHE-03 entero describe parsing de `meta.json` que no existe. Reescribir al contrato actual (corrupción = miss, próxima escritura sobreescribe).
8. **D-074** — RF-CACHE-04 (cleanup orphan uploads via DB query) NO está implementado. Decidir: implementar en Capa 5 vs downgrades a "Out of scope Capa 3". Disk leak real en prod si no se implementa.
9. **D-075** — Cache TTL en wiki dice `meta.created_at`, código usa `mtime`. Limpiar `RF-CACHE-02` Gherkin scenarios contradictorios.
10. **D-076** — Documentar `INTERNAL_ERROR` envelope shape (`{"detail": {"error_code", "reason", "error_id"}}`) y H-3 stripping policy en wiki.
11. **D-079** — `torch.cuda.empty_cache()` post-OOM no se llama; wiki promete recuperación. Agregar al orchestrator finally o relajar RF-TRX-05.
12. **D-082** — Eliminar `request_image_upload_url` y `attach_image` de RF-IMG (no existen como tools); colapsar bajo RF-MCP-01 (`kind="image"`).
13. **D-083** — `caption` en images queda NULL siempre (no hay write path). Decidir: agregar parameter a `request_upload_url` o dropear columna.
14. **D-084** — `start_transcription` acepta `num_speakers` no documentado en RF-MCP-02.
15. **D-085** — Crear ADR-018 (o el ID que siga) para justificar `enable_dns_rebinding_protection=False` en MCP transport (decisión de seguridad sin trazabilidad).
16. **D-059** — Implementar (o downgradar a aspiracional) los 9+ eventos de log estructurado wiki-spec'd: auth_login_started, auth_callback_received, auth_session_expired, mcp_bearer_generated, mcp_bearer_revoked, audio_normalized, cache_lookup, stt_completed, diarize_completed, merge_completed, cache_persisted, transcription_persisted, mcp_request_completed.
17. **D-060** — RF-AUTH-06 step 5 "auto-create bearer si no hay activo" no implementado. Decidir wiki edit vs code add.
18. **D-061** — `bearer.name` field en JSON responses no está en wiki schemas.

### Bloque 3 — MEDIUM (trazabilidad / consistencia)

19-35. (ver detalle por drift abajo): D-054 a D-058, D-062 a D-068, D-077, D-078, D-086, D-087, D-088.

### Bloque 4 — LOW (cosméticos)

36-40. D-069 a D-072, D-089.

### Gap declarado (fuera de drift numérico)

- **RF-UI-01 / RF-UI-02** marcadas como `Aprobado` en `04_RF.md` pero no existe `src/transcription_api/ui/` ni archivos `.tsx`/`.jsx`. La Capa 5 (UI web upload) no está implementada en el repo. Wiki debe flaggearlo explícitamente como "Pendiente Capa 5" en lugar de status `Aprobado`.

---

## Detalle de drifts

### Bloque ADRs + Top wiki (D-050 a D-058) — Agent 1

#### D-050 🔴 ADR-013 dice que `/api/upload` valida el bearer OAuth del MCP; código valida bearer efímero contra `upload_bearer_hash`

**Asumido (wiki)**: `wiki/ADR/ADR-013.md:30-35`:
- "El MCP server responde con `{upload_url, upload_id, bearer_token, expires_at}`"
- L33: "El endpoint POST /api/upload: **Valida el bearer (mismo OAuth Microsoft Entra del MCP)**."
- L56: "Bearer del OAuth ya valida la identidad; no se reintroduce un sistema de auth paralelo."

**Reality (código)**:
- `src/transcription_api/api/upload.py:114-132` — handler calcula `received_hash = hash_bearer(plaintext)` y compara contra `row.upload_bearer_hash` con `hmac.compare_digest`. El bearer del MCP NO se valida acá.
- `alembic/versions/1a4f8c9b2d6e_add_upload_bearer_hash.py` agregó la columna específicamente para esto (D-044 implementación).
- `wiki/05_modelo_datos.md:135` ya documenta `upload_bearer_hash` como bearer efímero "generado por `request_upload_url`", contradiciendo a ADR-013.
- `upload.py:20-29` comentario: "Auth model (G9 review-fix): This endpoint authenticates via the EPHEMERAL upload bearer ... NOT the ADR-014/015 scoping listener".

**Propagación**: ADR-013 §"Por qué A y no B" justifica la elección en que A "reutiliza el bearer ya gestionado por OAuth" — la justificación ya no aplica. El proyecto implementó la opción B (signed URLs temporales) sin reemplazar el ADR.

**Resolución sugerida**: Crear ADR-017 que reemplace ADR-013 reflejando el patrón implementado (ephemeral bearer hash) y marcar ADR-013 como `Reemplazada`. Alternativa: editar in-place suspendiendo regla de inmutabilidad (precedente D-009).

**Severidad rationale**: CRITICAL — un ADR `Aceptada` describe un modelo de auth factualmente incorrecto. Un dev usándolo como referencia para refactor de uploads confunde dos sistemas incompatibles y rompe el contrato del MCP tool `request_upload_url`.

---

#### D-051 🟠 `upload_session_grace_seconds` declarado dos veces en `config.py` con defaults distintos (30 vs 300)

**Asumido (wiki)**: `wiki/05_modelo_datos.md:141` define `expires_at = created_at + 10 min`. El campo grace está para "absorber clock skew" entre tool y REST endpoint (RF-MCP-02 step 6).

**Reality (código)**:
- `config.py:37-39` declara `upload_session_grace_seconds: int = Field(default=30, ..., ge=0)`.
- `config.py:155-157` redeclara `upload_session_grace_seconds: int = Field(default=300, ..., ge=0)`.
- Python class body semantics: la **segunda declaración pisa la primera** → default efectivo runtime = **300s**, no 30s.
- `api/upload.py:106-108, 254-256` consumen `settings.upload_session_grace_seconds` para extender ventana. Con default 300s, un upload puede llegar hasta 5 minutos después de `expires_at`, casi triplicando el TTL nominal (10 min).

**Propagación**: bug funcional silencioso. La ventana de validez es 10× la intendida.

**Resolución sugerida**: borrar la segunda declaración (líneas 155-157). Idealmente agregar test que valide unicidad de fields en `Settings`. Documentar el valor canónico en wiki/05 §2 o en `.env.example`.

**Severidad rationale**: HIGH — no causa data leak pero rompe semántica del TTL de upload session.

---

#### D-052 🔴 Macro: Taxonomía de error codes desalineada wiki ↔ código (3 capas afectadas)

**Asumido (wiki)** — `wiki/05_modelo_datos.md:362-402`:

Códigos canónicos listados: `INVALID_FORMAT`, `UNSUPPORTED_EXTENSION`, `FILE_TOO_LARGE`, `LOCK_BUSY`, `CUDA_OOM`, `MODEL_FAILURE`, `AUTH_NOT_AUTHENTICATED`, `AUTH_INVALID_OAUTH_CODE`, `AUTH_TENANT_NOT_ALLOWED`, `MCP_BEARER_INVALID`, `MCP_BEARER_REVOKED`, `UPLOAD_SESSION_ALREADY_CONSUMED`, `INTERNAL_ERROR`.

**Reality (código)** — facetas detectadas por los 3 agentes:

**(a) Pipeline (Agent 3)**: `api/transcriptions.py` emite códigos distintos:
- `AUDIO_TOO_LARGE` 413 (no `FILE_TOO_LARGE`)
- `AUDIO_FORMAT_INVALID` 400 (no `INVALID_FORMAT`/`UNSUPPORTED_EXTENSION`)
- `GPU_BUSY` 503 (no `LOCK_BUSY`)
- `GPU_ERROR` 500 con `extra.detail ∈ {oom, runtime}` (no `CUDA_OOM`/`MODEL_FAILURE` separados)
- `PIPELINE_NORMALIZE_ERROR` 500 (no documentado)
- `PIPELINE_DIARIZE_ERROR` 500 (no documentado)
- `PIPELINE_TIMEOUT` 504 (en data model pero no en RF-TRX)

**(b) ADRs+Top (Agent 1)**: extras en código no documentados:
- `DB_POOL_EXHAUSTED` (`main.py:445`)
- `REQUEST_TIMEOUT` (`main.py:466`)
- `UPLOAD_SESSION_ALREADY_CONSUMED` colapsa en `UPLOAD_SESSION_NOT_FOUND` por política de no-leak (AC-10 en `upload.py:97-103`).

**(c) MCP/IMG (Agent 4)**: `mcp/tools/upload.py:102-108` emite `INVALID_PARAMETER` cuando `mime_type` no está permitido. Wiki `RF-IMG.md:67` typed errors table dice `UNSUPPORTED_EXTENSION`. TP-IMG-01-neg-02 (wiki) testea `UNSUPPORTED_EXTENSION` que no existe.

**(d) Auth (Agent 2)**: `routes.py` emite `AUTH_INVALID_STATE` y `AUTH_PROVIDER_UNAVAILABLE` que aparecen en `04_RF.md:130-132` y `06_matriz_pruebas_RF.md:139, 141` pero NO en la taxonomía canónica de `05_modelo_datos.md §8`.

**Propagación**: rompe contrato externo en 4+ módulos. Cualquier MCP client que codée `if error_code == "LOCK_BUSY": retry` no matchea. Tests de contrato del catálogo viejo pasan en CI pero el endpoint no los emite.

**Resolución sugerida**: regenerar `wiki/05_modelo_datos.md §8` completo en una sola pasada:
- Eliminar: `LOCK_BUSY`, `CUDA_OOM`, `MODEL_FAILURE`, `UNSUPPORTED_EXTENSION`, `UPLOAD_SESSION_ALREADY_CONSUMED`.
- Agregar: `AUDIO_TOO_LARGE`, `AUDIO_FORMAT_INVALID`, `GPU_BUSY`, `GPU_ERROR` (con discriminador `detail`), `PIPELINE_NORMALIZE_ERROR`, `PIPELINE_DIARIZE_ERROR`, `PIPELINE_TIMEOUT`, `DB_POOL_EXHAUSTED`, `REQUEST_TIMEOUT`, `INVALID_PARAMETER`, `AUTH_INVALID_STATE`, `AUTH_PROVIDER_UNAVAILABLE`.
- Actualizar `wiki/RF/RF-TRX.md` §RF-TRX-03/04/05, `wiki/RF/RF-IMG.md` §Typed Errors, `wiki/06_matriz_pruebas_RF.md` cobertura por error_code.

**Severidad rationale**: CRITICAL — el `error_code` es contrato público con todos los consumers (MCP client de Claude, observability dashboards, tests, QA). Silent-leak de contrato cross-capa.

---

#### D-053 🟠 Wiki/02 §3 C4 ContainerDb describe cache "Por audio_hash"; código particiona por `(user_id, audio_hash)`

**Asumido (wiki)**:
- `wiki/02_arquitectura.md:57` — `ContainerDb(cache, "Caché Filesystem", "FS local", "Por audio_hash, TTL 24h")`.
- `wiki/02_arquitectura.md:113-114` — sequence diagram: `MCP->>FS: lookup cache audio_hash` (sin user_id).

**Reality (código)**:
- `src/transcription_api/pipeline/cache.py:65-67` — `_entry_path(user_id, audio_hash) → base_dir / str(user_id) / audio_hash / "result.json"`.
- `wiki/05_modelo_datos.md:14-22` ya documenta correctamente la estructura per-user (D-027 referenciado).
- `wiki/02_arquitectura.md:141` (tabla §5) sí refleja "per-user 24h (D-027)".

**Propagación**: las dos vistas (C4 §3 y sequence §4) están desactualizadas frente a D-027 ya documentado. Un dev leyendo wiki top-down se confunde con la inconsistencia entre §3, §4 y §5.

**Resolución sugerida**: editar `wiki/02_arquitectura.md` C4 en §3 (`Container(cache, ..., "Per-user (user_id, audio_hash), TTL 24h")`) y el sequence §4 para reflejar lookup con user_id.

**Severidad rationale**: HIGH — la C4 es el "primer encuentro" para un nuevo dev. Una propiedad de privacy crítica (aislamiento de cache entre users) mal pintada en el diagrama principal. No es CRITICAL porque el código sí enforza el aislamiento.

---

#### D-054 🟡 Wiki/05 §1 dice "TTL = file mtime"; §4 describe `CacheMeta` con `meta.json` separado

**Asumido (wiki)**:
- `wiki/05_modelo_datos.md:18` — `"TTL = file mtime"` y solo `result.json`.
- `wiki/05_modelo_datos.md:200-209` — §4 "Entidad `CacheMeta`" describe JSON con `audio_hash, duration_seconds, created_at, ttl_seconds, schema_version` en archivo `meta.json`.
- `wiki/ADR/ADR-004.md:27-31` — lista estructura `<hash>/transcription.json + meta.json`.

**Reality (código)**:
- `src/transcription_api/pipeline/cache.py:36` — único archivo es `_RESULT_FILENAME = "result.json"`. No hay `meta.json`.
- `cleanup.py` usa file mtime para TTL — coherente con §1.

**Resolución sugerida**: eliminar §4 `CacheMeta` de `wiki/05_modelo_datos.md` (o marcarlo deprecated → §1). Actualizar `ADR-004.md` con nota de que implementación final usa solo `result.json` + file mtime. Considerar versionado futuro: ADR-004 menciona `schema_version` en meta.json — al borrarlo, ¿cómo se migra el cache en futuras versiones?

**Severidad rationale**: MEDIUM — fricción documentación. Bajo riesgo runtime (código coherente consigo mismo) pero rompe trazabilidad SDD (wiki → código).

---

#### D-055 🟡 Componente `runtime/readiness.py` no documentado en wiki §2 ni §3 C4

**Asumido (wiki)**: `wiki/01_alcance_funcional.md §2` enumera componentes A-K. `wiki/02_arquitectura.md §3` muestra los mismos en C4 Container. Ninguno menciona Runtime Readiness Gate.

**Reality (código)**: `src/transcription_api/runtime/readiness.py` existe; `api/transcriptions.py:134`: `from ..runtime.readiness import check_models_ready`. Centraliza AC-15 (`MODELS_NOT_LOADED` 503) tanto para REST como MCP. Lógica de precedencia entre `whisper_status` y `pyannote_status` (G2 dedupe).

**Resolución sugerida**: agregar componente "L. Runtime Readiness Gate" en `wiki/01_alcance_funcional.md §2` (o sub-responsabilidad de "FastAPI App" en wiki/02 §5). Mencionar en C4 §3 como helper transversal.

**Severidad rationale**: MEDIUM — componente real con responsabilidad propia vive en módulo separado sin entrada en wiki. Fricción onboarding + `ps-trazabilidad`.

---

#### D-056 🟡 Wiki/02 §6 stack table dice "Python 3.10/3.11"; Dockerfile pinea solo 3.10

**Asumido (wiki)**:
- `wiki/02_arquitectura.md:149` — `"Pinear 3.10/3.11 en Dockerfile"`.
- `wiki/02_arquitectura.md:50` — C4: `"Python 3.10 + Uvicorn"`.
- `wiki/01_alcance_funcional.md:215` — `"Python 3.10–3.11"`.
- `pyproject.toml:9` — `requires-python = ">=3.10,<3.12"`.

**Reality (código)**: `Dockerfile:16-26` instala `python3.10` solamente, con `ln -sf /usr/bin/python3.10 /usr/bin/python`.

**Resolución sugerida**: alinear todo a "Python 3.10 fijo en imagen, pyproject permite 3.11 para dev local" — texto explícito en `wiki/02 §6`. Si se honra 3.11 en deployment, agregar `ARG PYTHON_VERSION` al Dockerfile.

**Severidad rationale**: MEDIUM — inconsistencia interna del wiki. Bajo riesgo (3.10 funciona), pero rompe "TODO explicit = 0".

---

#### D-057 🟡 Wiki/02 §6 stack table dice `mcp SDK Anthropic` sin versión; pyproject pinea `mcp[server]>=1.5,<2.0`

**Asumido (wiki)**: `wiki/02_arquitectura.md:150` — `"MCP Server | mcp SDK Anthropic + auth middleware custom"` sin versión.

**Reality (código)**: `pyproject.toml:47` — `"mcp[server]>=1.5,<2.0"`.

**Propagación**: ADR-011 menciona "SDK joven, breaking changes posibles | Pinear versión; tests de contrato" — el pin existe en código pero no se refleja en ADR ni wiki §6. Wiki §6 sí lista versiones para FastAPI 0.115, WhisperX 3.8.5, React 18, Vite 5 — pero omite la del MCP SDK (la más volátil).

**Resolución sugerida**: actualizar `wiki/02 §6` con `"mcp[server] 1.5.x (Anthropic SDK)"`. Mencionar pin en ADR-011 y condición bajo la cual se sube.

**Severidad rationale**: MEDIUM — el SDK con mayor riesgo de breaking change no tiene versión documentada.

---

#### D-058 🟢 ADR-006 dice imagen base `nvidia/cuda:12.1-cudnn8-runtime`; Dockerfile usa `12.1.1`

**Asumido (wiki)**: `wiki/ADR/ADR-006.md:26` — `"nvidia/cuda:12.1-cudnn8-runtime-ubuntu22.04"`.

**Reality (código)**: `Dockerfile:4` — `FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04`.

**Severidad rationale**: LOW — cosmético; `12.1.1` es patch release válido de 12.1.

---

### Bloque Auth (D-059 a D-072) — Agent 2

#### D-059 🟠 Macro: Logs estructurados wiki-spec'd no emitidos (auth + pipeline)

**Asumido (wiki)** — 2 fuentes complementarias:

**(a) Auth** — `wiki/05_modelo_datos.md:300-310` declara contractuales:
- `auth_login_started`, `auth_callback_received`, `auth_session_expired`, `mcp_bearer_generated`, `mcp_bearer_revoked`.

Confirmado por RF-AUTH-01 step 5, RF-AUTH-02 step 10, RF-AUTH-04 step 5, RF-AUTH-07 step 7.

**(b) Pipeline** — `wiki/05_modelo_datos.md:314-328` declara contractuales:
- `mcp_request_received`, `upload_url_requested`, `audio_normalized`, `cache_lookup`, `stt_completed`, `diarize_completed`, `merge_completed`, `cache_persisted`, `cache_persist_failed`, `transcription_persisted`, `mcp_request_completed`, `mcp_request_failed`, `lock_busy`, `cache_cleanup_completed` (con `entries_purged`, `bytes_freed`, `duration_ms`).

**Reality (código)**:

**(a) Auth**: `grep -r "auth_login_started\|auth_callback_received\|auth_session_expired\|mcp_bearer_generated\|mcp_bearer_revoked" src/` → 0 matches. `routes.py` solo emite `auth_user_created`, `auth_user_login`, `auth_logout` + error-id logs.

**(b) Pipeline**:
- `cleanup.py:115`: emite solo `"cache_cleanup_purged count=%d base=%s"` (no `cache_cleanup_completed`, no `bytes_freed`, no `duration_ms`).
- `orchestrator.py:154-345`: no emite ningún evento de fase.
- `transcriptions.py:178-182`: emite solo `legacy_endpoint_invoked` y errores vía `_stripped_500`.
- `upload.py:186`: emite `upload_received` (cumple) y `image_uploaded`.
- `cache.py`: zero logging.

**Propagación**: la wiki declara estos eventos "contractuales" (`wiki/05_modelo_datos.md:296`: "los RFs los referencian"). TP-TRX-01-cov-01 ("Cada paso de §Process Steps emite su log esperado") es infactible. SIEM/observability pipeline alineado a wiki perdería visibilidad.

**Resolución sugerida**: Opción A (recomendada): agregar logging estructurado al orchestrator y routes auth (1 línea por fase). Opción B: downgradar eventos a "Aspiracional Capa 5+" y eliminar referencias específicas de RFs.

**Severidad rationale**: HIGH — el contrato de auditabilidad (importante para Privacy = prioridad #1) está roto en 14 eventos.

---

#### D-060 🟠 RF-AUTH-06 step 5 "crear bearer si no hay activo" no implementado

**Asumido (wiki)**: `wiki/RF/RF-AUTH.md:397` Process Step 5: "Si no hay bearer activo (caso raro): crear uno (similar a RF-AUTH-04)".

**Reality (código)**: `routes.py:361-379` función `me()` setea `bearer_payload = None` si `bearer is None`. NO crea un bearer nuevo.

**Propagación**: user con bearer revocado (admin DB op o edge case) ve `bearer: null` y debe ir a POST `/auth/regenerate-mcp-token` manualmente — el banner de RF-AUTH-08 dice "iniciá sesión nuevamente para que se emita uno" pero RF-AUTH-04 dispara solo en primer login.

**Resolución sugerida**: dos opciones — (a) implementar auto-create en `me()`; (b) borrar step 5 de wiki, documentar que user debe ir a `/auth/regenerate-mcp-token` (más simple, alineado Privacy > Simplicity). Recomiendo (b).

**Severidad rationale**: HIGH — discrepancia funcional explícita entre process step y código.

---

#### D-061 🟠 `bearer.name` en JSON response de `/auth/me` y `/regenerate` no documentado

**Asumido (wiki)**: `wiki/RF/RF-AUTH.md:399, 458` schemas response listan `{id, plaintext_or_null, created_at}`. No mencionan `name`.

**Reality (código)**: `routes.py:378, 438` payload incluye `"name": bearer.name` (valores `"initial"` o `"regenerated"`).

**Resolución sugerida**: agregar `name` al schema response en RF-AUTH-06 step 7 y RF-AUTH-07 step 8.

**Severidad rationale**: HIGH — contrato API expone campo no documentado. Riesgo breaking change futuro si UI ya lo usa.

---

#### D-062 🟠 Error codes auth faltantes en taxonomía `05_modelo_datos.md §8`

Ver **D-052 (a) y (d)** consolidado: `AUTH_INVALID_STATE` y `AUTH_PROVIDER_UNAVAILABLE` aparecen en `04_RF.md:130-132` y `06_matriz_pruebas_RF.md:139, 141` pero no en taxonomía canónica `05_modelo_datos.md:362-368`.

**Resolución sugerida**: ya cubierta por D-052 fix consolidado.

**Severidad rationale**: HIGH (parte del macro-drift).

---

#### D-063 🟡 TP-AUTH-08 (RF-AUTH-08 test traceability) sin sección correspondiente en TP-AUTH.md

**Asumido (wiki)**: `wiki/RF/RF-AUTH.md:594-599` Test Traceability de RF-AUTH-08 lista `TP-AUTH-08-pos-01/02`, `-neg-01`, `-int-01`.

**Reality (wiki)**: `wiki/pruebas/TP-AUTH.md` cubre TP-AUTH-01 a TP-AUTH-07. No tiene sección TP-AUTH-08. `wiki/06_matriz_pruebas_RF.md` tampoco mapea RF-AUTH-08 → tests.

**Resolución sugerida**: agregar sección "TP-AUTH-08: Banner UI sobre estado del bearer" en `wiki/pruebas/TP-AUTH.md`, o si es UI-only referenciar desde RF-AUTH-08 → "ver TP-UI".

**Severidad rationale**: MEDIUM — RF-AUTH-08 "Aprobado" con test plan vapor. Rompe "TODO explicit = 0" de CLAUDE.md §11.

---

#### D-064 🟡 POST `/auth/logout` no tiene RF dedicado

**Asumido (wiki)**: state machine y log event `auth_logout` mencionados, pero ningún RF describe el endpoint (request shape, response status, cookies a borrar, side-effects).

**Reality (código)**: `routes.py:459-476` `logout()` endpoint: 302 redirect a `/login`, delete_cookie `session`, log `auth_logout` (sin user_id). ALT-1: NO revoca bearer MCP.

**Resolución sugerida**: agregar RF-AUTH-09 "POST /auth/logout — borra cookie de sesión" con contrato completo + ALT-1 cross-link a RF-AUTH-08 + TP-AUTH-09.

**Severidad rationale**: MEDIUM — endpoint productivo sin RF rompe SoT.

---

#### D-065 🟡 `auth_logout` log no emite `user_id` (wiki-spec'd)

**Asumido (wiki)**: `wiki/05_modelo_datos.md:307` `auth_logout | INFO | user_id | POST /auth/logout`.

**Reality (código)**: `routes.py:475` emite `logger.info("auth_logout")` sin user_id (endpoint no usa `Depends(get_current_user_web)` intencionalmente).

**Resolución sugerida**: (a) decode best-effort de cookie pre-delete; (b) updatear wiki a `request_id`. Recomiendo (a) para preservar auditoría.

**Severidad rationale**: MEDIUM — contrato log roto. Acoplado a D-064.

---

#### D-066 🟡 RF-AUTH-02 step 6 no documenta fallback `email → preferred_username`

**Asumido (wiki)**: `wiki/RF/RF-AUTH.md:155`: "Extraer `oid`, `email`, `name` del `id_token`".

**Reality (código)**: `routes.py:235`: `email = claims.get("email") or claims.get("preferred_username")`. Si ambos faltan → `AUTH_PROVIDER_UNAVAILABLE`. Documentado en código como fix H-9.

**Resolución sugerida**: actualizar RF-AUTH-02 step 6: "Extraer `oid`; `email` con fallback a `preferred_username` (H-9); si ambos ausentes → `AUTH_PROVIDER_UNAVAILABLE`".

**Severidad rationale**: MEDIUM — fail-closed correcto, pero contrato `email` documentado es más restrictivo que realidad.

---

#### D-067 🟡 RF-AUTH no referencia ADR-014/015 (per-user scoping fail-closed)

**Asumido (wiki)**: ADR-015 indica que `get_current_user_*`, `verify_bearer`, `callback` están envueltos en `bypass_scoping`. RF-AUTH-06/07 dependen de que el listener esté armado por `_arm_session`.

**Reality (wiki)**: `grep "ADR-014\|ADR-015\|bypass_scoping\|ScopingNotArmedError" wiki/RF/RF-AUTH.md wiki/FL/FL-AUTH-01.md` → 0 matches.

**Resolución sugerida**: actualizar FL-AUTH-01 §8 con ADR-014/015. Agregar nota en RF-AUTH-06/07 Process Steps sobre el arming de `session.info['user_id']`.

**Severidad rationale**: MEDIUM — gap trazabilidad RF↔ADR. Integridad del invariante depende de mecanismo no mencionado.

---

#### D-068 🟡 TP-AUTH §Convenciones no documenta marker `requires_docker`

**Asumido (wiki)**: `wiki/pruebas/TP-AUTH.md:5-11` "Convenciones" lista pytest 8.x + asyncio + httpx + freezegun + responses. No menciona markers.

**Reality (código)**: todos los tests integration `tests/integration/auth/` usan `pytestmark = pytest.mark.requires_docker`.

**Resolución sugerida**: agregar bullet en TP-AUTH §Convenciones: "Tests Integration usan marker `requires_docker` — auto-skip sin Docker (testcontainers Postgres). Ver tests/conftest.py y CLAUDE.md §12".

**Severidad rationale**: MEDIUM — afecta confiabilidad de suite en máquinas dev sin docker.

---

#### D-069 🟡 FL-AUTH-01 §7 sigue mostrando "400/403/502 +" status codes pre-redirect

**Asumido (wiki)**: `wiki/FL/FL-AUTH-01.md:85-90` tabla "errores":
- "400 + redirect `/login?error=AUTH_INVALID_STATE`"
- "403 + redirect `/login?error=AUTH_TENANT_NOT_ALLOWED`"
- "502 + redirect `/login?error=AUTH_PROVIDER_UNAVAILABLE`"

**Reality (código)**: `routes.py` retorna consistentemente `RedirectResponse(status_code=302)` para los 4 casos. El "código semántico" viaja en query param.

**Propagación**: D-013 cerró este drift en RF-AUTH.md, pero la tabla en FL-AUTH-01 §7 no fue actualizada.

**Resolución sugerida**: editar `wiki/FL/FL-AUTH-01.md:85-90` reemplazando "400 + redirect" → "302 redirect a `/login?error=AUTH_INVALID_STATE` (semántico 400)".

**Severidad rationale**: MEDIUM — FL-AUTH-01 es flujo origen referenciado por RFs AUTH; inconsistencia degrada fidelidad.

---

#### D-070 🟢 Wiki no documenta `SESSION_TTL_SECONDS` env var

**Asumido (wiki)**: TTL session hardcoded como "24h" en `wiki/RF/RF-AUTH.md:159`, `wiki/pruebas/TP-AUTH.md:75-78`, `wiki/FL/FL-AUTH-01.md:125`.

**Reality (código)**: `config.py:94-96` configurable vía `SESSION_TTL_SECONDS`, default `86400` (24h). `.env.example:80` lo documenta.

**Resolución sugerida**: cambiar wiki "TTL 24h" → "TTL configurable vía `SESSION_TTL_SECONDS`, default 86400 (24h)".

**Severidad rationale**: LOW — default coincide; afecta solo deploys que tuneen.

---

#### D-071 🟢 Wiki no documenta validación mínima 32 chars de `JWT_SECRET`

**Asumido (wiki)**: `wiki/RF/RF-AUTH.md:237` "closed decisions: JWT firmado HS256 con `JWT_SECRET`". `TP-AUTH.md:10` muestra `JWT_SECRET=<test-secret>`.

**Reality (código)**: `config.py:104-119` valida `len(raw) < 32` → ValueError boot-time.

**Resolución sugerida**: agregar nota en RF-AUTH-02 "Closed decisions": "`JWT_SECRET` ≥32 chars (boot-validated). Generar con `python -c 'import secrets; print(secrets.token_urlsafe(48))'`".

**Severidad rationale**: LOW — UX dev.

---

#### D-072 🟢 Wiki no documenta JWKS retry-on-signature-failure (CR-1)

**Asumido (wiki)**: `wiki/RF/RF-AUTH.md:262`: "Validar firma con JWKS (descarga y cacheo)". No menciona retry.

**Reality (código)**: `routes.py:185-211` + `oauth_client.py:154-184` implementan double-attempt con `force_refresh=True` ante `IdTokenInvalid`. Comentario CR-1 documenta el caso de MS key rotation.

**Resolución sugerida**: agregar a RF-AUTH-03 Special Cases: "JWKS key rotation: ante signature failure, force-refresh + retry una vez antes de `AUTH_PROVIDER_UNAVAILABLE`. Cache TTL 24h, asyncio.Lock serializa refreshes (H-5)".

**Severidad rationale**: LOW — comportamiento defensivo, no afecta contrato externo.

---

### Bloque Pipeline (D-073 a D-080) — Agent 3

#### D-073 🟠 RF-CACHE-03 describe `meta.json` que ya no existe (D-027 incompleto)

**Asumido (wiki)**: `wiki/RF/RF-CACHE.md:291-298` Process Steps describe parser de `<hash>/meta.json` con campos `created_at`, `ttl_seconds`, `schema_version`, eventos `cache_meta_unreadable`.

**Reality (código)**: `cache.py:65-92` + `cleanup.py:31, 44-56, 82-83`:
- Único archivo: `<user_id>/<audio_hash>/result.json`. No hay `meta.json`.
- TTL derivado de `os.stat(result.json).st_mtime`.
- Cleanup nunca abre JSON; solo stat + unlink.
- `cache.get()` trata corrupción como miss (return None).

**Resolución sugerida**: reescribir RF-CACHE-03 al contrato actual: "entrada corrupta = JSON parsea mal en `cache.get()` → miss, próxima escritura sobreescribe. Cleanup no inspecciona contenido, solo mtime". Eliminar §4 CacheMeta en `05_modelo_datos.md`. Eliminar referencias a `schema_version`.

**Severidad rationale**: HIGH — RF execution-normative describe mecanismo inexistente. Dev nuevo construye expectativas falsas.

---

#### D-074 🟠 RF-CACHE-04 (upload_sessions cleanup) NO implementado

**Asumido (wiki)**: `wiki/RF/RF-CACHE.md:371-493` RF-CACHE-04 mandatory: "sin esto, disco crece sin límite con uploads abandonados". Execution-Normative. Spec: query Postgres `status IN ('requested','uploaded') AND expires_at + grace < now`, UPDATE a `expired`, DELETE filesystem `<DATA_DIR>/uploads/<upload_id>/`, eventos `upload_session_expired`, `upload_session_cleanup_completed`.

**Reality (código)**: `cleanup.py:59-117` + `main.py:244-274`:
- `purge_expired(base_dir, ttl_seconds)` SOLO toca cache filesystem (`<base>/<user>/<hash>/result.json`).
- No abre Postgres; no querya `upload_sessions`; no toca `<DATA_DIR>/uploads/`.
- `main.py:317-344` `_purge_orphan_uploads()` SÍ borra archivos en `uploads_dir`, pero **solo al startup**, no periódicamente. Y NUNCA marca rows como `expired`.

**Propagación**: feature mandatory ausente. Disk leak documentado: sessions `uploaded` que jamás se consumen → bytes en `uploads/<id>/original.bin` huérfanos. Privacy regression (prioridad #1).

**Resolución sugerida**: decidir si RF-CACHE-04 se implementa en Capa 5 o se downgrade a "Out of scope Capa 3". Si lo segundo, agregar nota status `Pendiente Capa 5`. Si lo primero: extender `_cleanup_loop()` con query Postgres + DELETE filesystem.

**Severidad rationale**: HIGH — disk leak real + privacy regression para prioridad #1 del proyecto.

---

#### D-075 🟠 Cache TTL semantics — RF-CACHE-02 dice `meta.created_at`, código usa `mtime`

**Asumido (wiki)**:
- `wiki/RF/RF-CACHE.md:163, 211-216`: "Si `now() - meta.created_at > meta.ttl_seconds`, ya no existen en disco."
- §Process Step 3b dice mtime (alineado D-027), pero §Postcondiciones y Gherkin scenarios siguen con `created_at`.

**Reality (código)**: `cleanup.py:70, 83, 93`:
```python
cutoff = time.time() - ttl_seconds
mtime = result_file.stat().st_mtime
if mtime >= cutoff: continue
```

Cache write en `cache.py:81-92` no setea mtime explícitamente — queda default del filesystem en `tmp.replace(path)`.

**Resolución sugerida**: limpiar contradicción interna de RF-CACHE-02 — quitar todas las referencias a `created_at` parseado; reemplazar por "mtime del result.json". Eliminar scenario "future-dated" o cambiar a "mtime > now" (caso de drift de reloj del FS).

**Severidad rationale**: HIGH — contradicción interna en RF normativo. Tests TP-CACHE-02-pos-03 (future-dated) no inducibles contra código actual.

---

#### D-076 🟠 `INTERNAL_ERROR` shape y `error_id` no documentados en wiki

**Asumido (wiki)**: `wiki/RF/RF-TRX.md:113` define respuesta `INTERNAL_ERROR` como `HTTP 500 + {"error_code": "INTERNAL_ERROR", "request_id": "..."}`.

**Reality (código)**: `api/transcriptions.py:91-123` (`_stripped_500`):
```python
body = {"error_code": code, "reason": "see error_id in logs", "error_id": error_id}
return JSONResponse(status_code=500, content={"detail": body})
```
- Body envuelto bajo `"detail"` (FastAPI convention).
- `request_id` documentado no existe; reemplazado por `error_id` (UUID por error, no por request).
- `reason` siempre literal `"see error_id in logs"` — H-3 stripping policy.

**Resolución sugerida**: documentar shape canónica `{"detail": {"error_code", "reason", "error_id"}}` en `wiki/05_modelo_datos.md` nueva sección "Error response envelope". Documentar H-3 stripping policy (qué errores leakean `str(exc)` — solo `AudioFormatInvalid` — y cuáles devuelven `"see error_id in logs"`). Reemplazar `request_id` por `error_id` en RF-TRX-01/05.

**Severidad rationale**: HIGH — respuesta HTTP es contrato público. MCP client que parsee `response["error_code"]` (sin `.detail`) no encuentra el campo. Confusión `request_id` vs `error_id` bloquea auditoría de incidentes.

---

#### D-077 🟡 `MODEL_FAILURE` no se distingue en código; toda GPU error mappea a `GPU_ERROR`

**Asumido (wiki)**: `wiki/RF/RF-TRX.md:577-580` dos códigos separados: `CUDA_OOM` (operador: reducir batch) vs `MODEL_FAILURE` (operador: revisar driver).

**Reality (código)**: `stt.py:128-129, 145-172, 203-210` + `transcriptions.py:280-285`: `GPUError` con `detail ∈ {"oom", "runtime"}`, todo mappea a `GPU_ERROR` con `extra={"detail": exc.detail}`. `diarize.py:280` toda excepción pyannote → `PipelineDiarizeError` → `PIPELINE_DIARIZE_ERROR`.

**Resolución sugerida**: la separación as-built es razonable (discriminator `detail`). Sincronizar RF-TRX-05: reemplazar §Typed Errors con `GPU_ERROR` + `detail ∈ {oom, runtime}` y `PIPELINE_DIARIZE_ERROR`. Eliminar `MODEL_FAILURE`.

**Severidad rationale**: MEDIUM — bloquea solo tests TP-TRX-05-neg-03; workaround ya existe con `detail`. Parte del macro-drift D-052.

---

#### D-078 🟡 Campo metadata: wiki dice `served_from_cache`, código emite `cache_hit`

**Asumido (wiki)**: `wiki/RF/RF-TRX.md:247` + `wiki/05_modelo_datos.md:180`:
```json
"metadata": {"audio_hash": "...", "served_from_cache": false}
```

**Reality (código)**: `orchestrator.py:226-233, 257-263, 290-298`:
```python
"metadata": {"cache_hit": True, ...}
```

Campo es `cache_hit`, no `served_from_cache`. Coherente con log estructurado `mcp_request_completed.cache_hit` en data model §7.

**Resolución sugerida**: reemplazar `served_from_cache` por `cache_hit` en wiki/05 §3 y RF-TRX-02.

**Severidad rationale**: MEDIUM — cliente MCP con `payload["metadata"]["served_from_cache"]` recibe KeyError.

---

#### D-079 🟠 RF-TRX-05 dice "ejecutar `torch.cuda.empty_cache()`" post-OOM; código no lo hace

**Asumido (wiki)**: `wiki/RF/RF-TRX.md:561` RF-TRX-05 Step 3: "Si `torch.cuda.OutOfMemoryError`: ejecutar `torch.cuda.empty_cache()`, emitir log ERROR".

§Acceptance Criteria Scenario "Recuperación tras OOM": "Given un request A produjo CUDA_OOM, when un cliente B hace POST /transcribe inmediatamente, then B procesa normalmente (lock disponible, VRAM liberada)".

**Reality (código)**: `stt.py:202-210` propaga `GPUError(DETAIL_OOM, str(exc))`. `orchestrator.py:333-344 finally` solo limpia el WAV normalizado. Búsqueda completa: el único `empty_cache()` está en `main.py:385` durante shutdown.

**Propagación**: en 8GB VRAM con `int8_float16`, un OOM puntual deja fragmentación. Sin `empty_cache()` el próximo request puede OOM-ear también. ADR-001 dice "VRAM ~7-8 GB, ajustado pero entra" → sin margen.

**Resolución sugerida**: agregar `torch.cuda.empty_cache()` en orchestrator finally cuando `_run_pipeline` levantó `GPUError(detail="oom")` (lazy import). Alternativa: relajar RF-TRX-05 Step 3 ("torch's allocator reusa bloques internamente; operador debe reiniciar container si VRAM no se recupera tras N OOMs consecutivos").

**Severidad rationale**: HIGH — propiedad RF-TRX-05 ("VRAM liberada") no garantizada por código en hardware con margen estrecho.

---

#### D-080 🔴 Audio format whitelist diverge + magic-bytes fallback no documentado

**Asumido (wiki)**: `wiki/RF/RF-TRX.md:60` `file: Extensión en {mp4, mp3, wav, m4a, flac}`. L349 "responder 400 + `UNSUPPORTED_EXTENSION`". L421 "no se confía en `Content-Type` HTTP; sólo en extensión + intento de ffmpeg" — guard es **suffix-only**.

**Reality (código)**: `normalize.py:41, 166-214, 281-316`:
- Whitelist matchea: `_ALLOWED_EXTENSIONS = {"mp3", "mp4", "m4a", "wav", "flac"}`.
- PERO: si extensión NO está en whitelist → llama `_detect_ext_by_magic(head)` (fallback). Si magic-byte coincide con conocido (`RIFF/WAVE`, `fLaC`, `ID3`, `0xFFE`, `ftyp`), **continúa el flujo** con extensión inferida.
- El error que llega al cliente NO es `UNSUPPORTED_EXTENSION`, es `AUDIO_FORMAT_INVALID`.
- Comentario `normalize.py:299-306`: el fallback existe por D-048 (upload endpoint persiste `original.bin` sin extensión).

**Propagación**: contrato wiki "rechaza extensión no en whitelist" se viola intencionalmente para soportar chunked upload de Capa 4. TP-TRX-03-neg-02 ("`.txt` → 400 UNSUPPORTED_EXTENSION") falla si `.txt` contiene MP3 válido — código lo ACEPTA.

**Resolución sugerida**: documentar magic-byte fallback en RF-TRX-03 como §Special Case explícito ("D-048: upload endpoint guarda como `original.bin`; normalize.py recurre a magic-byte detection si la extensión no matchea; rechaza solo si extensión + magic-byte ambos fallan"). Actualizar §Closed Decisions: "no se valida magic bytes" → "sí se validan magic bytes para containers conocidos (RIFF, fLaC, ID3, 0xFFE, ftyp)". Sincronizar TP-TRX-03-neg-02.

**Severidad rationale**: CRITICAL — bypass de validation contract. Wiki promete propiedad de seguridad que código no cumple. Atacante puede smuggle audio con cualquier extensión.

---

### Bloque MCP + UI + DB (D-081 a D-089) — Agent 4

#### D-081 🔴 MCP URL pública: wiki dice `/mcp`, código sirve en `/mcp/mcp`

**Asumido (wiki)**:
- `wiki/RF/RF-MCP.md:38`: "URL pública: `${PUBLIC_BASE_URL}/mcp`".
- `wiki/ADR/ADR-011.md:60`: "`GET /mcp` MCP server endpoint".

**Reality (código)**:
- `main.py:429` monta `app.mount("/mcp", mcp_app)` donde `mcp_app = mcp_server.streamable_http_app()` (`mcp/__init__.py:35`).
- `FastMCP.streamable_http_app()` por default sirve en `/mcp` interno (`.venv/.../mcp/server/fastmcp/server.py:166`, `streamable_http_path: str = "/mcp"`).
- Resultado: URL real es `${PUBLIC_BASE_URL}/mcp/mcp`.
- `auth/routes.py:64-66` `_mcp_url()` retorna `f"{settings.public_base_url.rstrip('/')}/mcp"` — y `/auth/me` lo expone como `mcp_url`. La UI genera snippets Claude config con URL inválido.

**Propagación**: cualquier cliente que use `mcp_url` de `/auth/me` (UI snippet → user copia config Claude) recibe 404. Feature MCP no funciona out-of-the-box para nuevos users sin que alguien edite manualmente la URL.

**Resolución sugerida**:
- Opción A (recomendada): pasar `streamable_http_path="/"` a `FastMCP(...)`, así el mount en `/mcp` queda en URL `${PUBLIC_BASE_URL}/mcp` real (consistente con wiki).
- Opción B: documentar explícitamente `${PUBLIC_BASE_URL}/mcp/mcp` y ajustar `_mcp_url()` para concatenar `/mcp/mcp`.

**Severidad rationale**: CRITICAL — bloquea onboarding de nuevos users. Tu setup actual funciona porque tu `~/.claude.json` ya tiene `/mcp/mcp` hardcoded.

---

#### D-082 🟠 Tools `request_image_upload_url` + `attach_image` no existen en código

**Asumido (wiki)**:
- `wiki/RF/RF-IMG.md:11-15` define 3 tools: `request_image_upload_url` (RF-IMG-01), `POST /api/upload-image` (RF-IMG-02), `attach_image` (RF-IMG-03).
- `wiki/FL/FL-IMG-01.md:18-21` Actores: "Tools `request_image_upload_url`, `attach_image`".
- `wiki/ADR/ADR-011.md:35-36` lista las 2 tools.

**Reality (código)**: `mcp/tools/__init__.py` registra 7 tools: `delete_transcription, get_transcription, list_my_transcriptions, search_my_transcriptions, start_transcription, request_upload_url, get_user_info`. **No existen** `request_image_upload_url` ni `attach_image`. La lógica image quedó unificada en `request_upload_url(kind="image", transcription_id, mime_type, file_size_bytes)`. `POST /api/upload-image` hace la inserción en `images` directamente, sin attach posterior. `caption` nunca se setea.

**Propagación**: D-043 documenta parcialmente esto desde la perspectiva ADR-011, pero RF-IMG.md sigue describiendo la API vieja.

**Resolución sugerida**: refactor `wiki/RF/RF-IMG.md` colapsando RF-IMG-01 + RF-IMG-03 en nota "Cubierto por RF-MCP-01 (kind=image)". Actualizar `wiki/FL/FL-IMG-01.md` §6 (eliminar `attach_image` del diagrama).

**Severidad rationale**: HIGH — wiki describe API inexistente. Dev nuevo busca funciones no registradas.

---

#### D-083 🟠 `caption` en images queda dead — no hay write path

**Asumido (wiki)**: `wiki/RF/RF-IMG.md:198-205` RF-IMG-03 Step 4: "UPDATE images SET caption=? (si caption presente)". `wiki/05_modelo_datos.md:115` images table: "`caption` TEXT NULL — Caption opcional para minuta". FL-MIN-01 asume captions disponibles para contextualizar minutas con imágenes.

**Reality (código)**: no existe tool `attach_image`. `api/upload.py:310-321` inserta `images` sin caption. `request_upload_url` no acepta caption. `images.caption` permanece NULL para siempre.

**Resolución sugerida**: opciones —
1. Agregar `caption?` parameter a `request_upload_url(kind="image", ..., caption=...)`, propagar a `upload_sessions`, persistir en INSERT image.
2. Agregar tool `set_image_caption(image_id, caption)` post-upload.
3. Aceptar que captions no se exponen y dropear columna + serializer.

**Severidad rationale**: HIGH — feature funcional ausente que FL-MIN-01 asume.

---

#### D-084 🟠 `start_transcription` acepta `num_speakers` no documentado en RF-MCP-02

**Asumido (wiki)**: `wiki/RF/RF-MCP.md:230-237` RF-MCP-02 inputs: `upload_id, language, max_speakers, min_speakers`. No menciona `num_speakers`.

**Reality (código)**: `mcp/tools/start.py:126-132`:
```python
async def start_transcription(
    upload_id: str,
    language: str = "es",
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
)
```

`num_speakers` se forwardea a `orchestrate(...)` (l. 204). Cliente MCP lo ve por introspection del SDK pero la doc lo oculta.

**Resolución sugerida**: agregar `num_speakers` (int opcional 1-16) a tabla inputs en RF-MCP-02, documentar precedencia sobre min/max si presente, agregar TP-MCP-02-pos-03.

**Severidad rationale**: HIGH — drift en API surface, input público no documentado.

---

#### D-085 🟠 `enable_dns_rebinding_protection=False` no documentado como ADR

**Asumido (wiki)**: RF-MCP-00 transport menciona "Streamable HTTP", auth bearer + per-user scoping. NINGÚN ADR (008-016) menciona DNS rebinding protection ni Host header check disabling.

**Reality (código)**: `mcp/server.py:30-37`:
```python
_TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=False,
)
mcp_server: FastMCP = FastMCP(name="transcription-api", transport_security=_TRANSPORT_SECURITY)
```

Docstring justifica ("intranet-only, bearer-authenticated server"). Trade-off real de seguridad (desactivar defense-in-depth) que merece ADR.

**Resolución sugerida**: crear ADR-017 (o el ID que siga) "Desactivación de DNS rebinding protection en MCP transport": contexto (intranet ZeroTier/Tailscale/LAN), riesgo aceptado, mitigaciones (bearer auth + red privada), trigger de revisión (exposición pública). Referenciar desde RF-MCP-00 §Transporte.

**Severidad rationale**: HIGH — decisión de seguridad sin trazabilidad formal. Privacy = prioridad #1. Un futuro deploy público sin revisar = vulnerabilidad real.

---

#### D-086 🟡 `text_content` column attribute renombrado vs JSON Schema

**Asumido (wiki)**: `wiki/05_modelo_datos.md:95` table `transcriptions` columna `text TEXT NOT NULL`. JSON Schema (l. 156-186) `TranscriptionResult` muestra `segments`, `metadata`, `transcription_id`, etc. — **no incluye campo `text` ni `text_content`**.

**Reality (código)**: `db/models/transcription.py:56`:
```python
text_content: Mapped[str] = mapped_column("text", Text, nullable=False)
```

Atributo Python `text_content`, columna DB `text`. Serializer `mcp/serializers.py:88` expone como `"text_content": row.text_content` en `serialize_full`. Esta key NO aparece en JSON Schema documentado.

**Resolución sugerida**: agregar `text_content` al JSON Schema en `wiki/05_modelo_datos.md §3`. Considerar renombrar atributo Python a `text` (Mapped[str] no requiere el alias).

**Severidad rationale**: MEDIUM — campo en payload real ausente del schema; API consumer recibe campo no documentado.

---

#### D-087 🟡 Caption schema dead-code en images table

**Asumido (wiki)**: `wiki/05_modelo_datos.md:127-148` `upload_sessions` no menciona `caption`. `wiki/RF/RF-IMG.md:46` `request_image_upload_url` inputs incluyen `filename` opcional, **no caption**.

**Reality (código)**: consecuencia de D-083. `images.caption` queda como columna decorativa.

**Resolución sugerida**: ver D-083. Si se conserva caption: ampliar `wiki/05_modelo_datos.md upload_sessions` con `expected_caption TEXT NULL` + parameter a `request_upload_url`. Si se elimina: ADR + drop column migration.

**Severidad rationale**: MEDIUM — schema dead-code; bajo impacto runtime, baja confianza en data model.

---

#### D-088 🟡 `transcription_resource` UUID validation pattern inconsistente vs `image_resource`

**Asumido (wiki)**: `wiki/RF/RF-MCP.md:525-538` RF-MCP-07: "Equivalente a RF-MCP-06 pero servido como Resource MCP" — thin reuse.

**Reality (código)**: `resources.py:37-45` `transcription_resource` delega a `get_transcription` (validación UUID interna). `image_resource:48-95` hace su propia validación UUID (l. 60-74). Duplicación de lógica que la wiki dice ser thin reuse.

**Resolución sugerida**: extraer helper `_parse_uuid_or_400(value, name)` a `mcp/lookup.py` y usar desde ambos resources + tools que validan UUID. Actualizar RF-MCP-07/08 mencionando que cada resource tiene validación propia o que comparten helper.

**Severidad rationale**: MEDIUM — code-smell, no bug funcional. Flag por discrepancia wiki ("thin reuse") vs realidad.

---

#### D-089 🟢 `idx_transcriptions_audio_hash` index documentado pero ningún tool lo usa

**Asumido (wiki)**: `wiki/05_modelo_datos.md:103` "Index: `idx_transcriptions_audio_hash` (audio_hash)" — y migración `352c7acf6f15` línea 76 lo crea.

**Reality (código)**: ningún tool MCP querya `transcriptions` por `audio_hash`. `list/search/get/delete/start` filtran por `id`, `user_id`, `text_content`, `created_at`. Cache filesystem usa audio_hash pero contra disco, no DB. Índice presente pero ningún query lo aprovecha.

**Resolución sugerida**: decidir use case. Opción A: agregar tool/use case (e.g. analytics "transcripciones que comparten audio_hash"). Opción B: drop el índice en migración futura.

**Severidad rationale**: LOW — cosmético; storage overhead bajo.

---

## Gap declarado (fuera de drift numérico)

### RF-UI-01 / RF-UI-02 marcadas `Aprobado` pero no implementadas

**Asumido (wiki)**: `wiki/04_RF.md` y `wiki/RF/RF-UI.md` describen páginas React (Vite bundle servido por FastAPI StaticFiles): `/login`, `/mcp-setup`. Status: `Aprobado`.

**Reality (código)**: no existe `src/transcription_api/ui/` ni archivos `.tsx`/`.jsx`. `find . -name "*.tsx"` retorna vacío. El backend expone `/auth/me` y `/mcp-setup` (este último solo renderiza un HTML mínimo desde routes.py, NO una SPA React).

**Resolución sugerida**: cambiar status de RF-UI-01/RF-UI-02 a "Pendiente Capa 5" en `04_RF.md` y `RF-UI.md`. Documentar en `01_alcance_funcional.md §3` que la UI web está deferida.

**Severidad**: HIGH (gap funcional declarado como done en wiki). Lo dejo fuera del numerado para señalar que es deuda de scoping, no drift wiki ↔ código.

---

## Patrones / lecciones consolidadas

Tras 4 capas auditadas, emergen 5 patrones recurrentes que merecen ser parte del runbook SDD del proyecto:

1. **Taxonomía de error codes drifta first**: cada vez que una capa agrega features (Capa 2 auth, Capa 3 pipeline, Capa 4 MCP), introduce error codes nuevos que el §8 de `05_modelo_datos.md` no captura. Patrón de mitigación: **antes de mergear cada capa, hacer un `grep -rE "(error_code|raise_tool_error)" src/` y diff vs §8**.

2. **JSON Schemas en wiki quedan stale en pequeños campos**: `text_content`, `cache_hit` vs `served_from_cache`, `bearer.name`, `num_speakers` — todos son campos nuevos al payload que la doc no captura. Patrón: **generar el schema desde código (pydantic `.model_json_schema()`) y compararlo contra wiki como gate de PR**.

3. **C4 / diagramas viejos sobreviven decisiones nuevas**: cache "por audio_hash" vs `(user_id, audio_hash)` (D-027 aplicado al texto pero no al diagrama). Patrón: **cuando hay decisión que modifica una propiedad capturada en C4, regenerar el diagrama en el mismo PR**.

4. **Logs estructurados wiki-spec'd no se emiten**: 9+ eventos en pipeline + 5 en auth. La wiki declara contractuales eventos que el código no produce, ergo TP-* que los chequean son infactibles. Patrón: **gate de logging en CI — wiki dice evento X → grep `logger.info\(.*X` en src/ debe matchear; sino, error**.

5. **RFs cementan claims que dependen de un único path de código**: RF-AUTH-06 step 5 "auto-create bearer" depende de implementación que nunca llegó. Patrón: **cualquier RF step que diga "si X entonces hacer Y" debe tener un test traceability ID; si no, downgradar a "Special case (no implementado)" en lugar de pretender que es execution-normative**.

---

## Cierre

Trabajo siguiente sugerido (orden):

1. **Tocar `wiki/05_modelo_datos.md §8`** una sola vez para resolver D-052 (4 capas, 1 PR).
2. **Decidir D-074** (RF-CACHE-04 implementar vs downgrade) — bloquea cierre del modelo de cache.
3. **Aplicar fix D-081** (`streamable_http_path="/"`) — corregir tu config en `~/.claude.json` para usar `/mcp` simple y verificar que el rig sigue funcionando.
4. **Crear ADR-017** (reemplaza ADR-013) + **ADR-018** (DNS rebinding) en un PR conjunto.
5. **Re-graphify** (`/graphify --update`) tras 1-4 aplicados — el grafo `manifest.json` muestra Apr 30, ahora hay 16 ADRs vs 7, 6 FLs vs 2, 6 RFs vs 2. El refresh es prerequisito para que próximos audits usen el grafo como mapa rough.
